"""
Catalog Service - Business Logic Microservice
Responsabil cu logica de business pentru catalogul de jocuri:
- Listare jocuri cu filtre (categorie, dificultate, jucatori, search)
- Detalii joc individual
- Recomandari pe baza de criterii
- Verificare stoc

Acest serviciu nu expune endpoint-uri de scriere catre DB - doar citeste.
"""
import os
import logging
import psycopg2
import psycopg2.extras
import jwt
from flask import Flask, request, jsonify
from functools import wraps
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import time

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [catalog-service] %(message)s'
)
logger = logging.getLogger(__name__)

# ---------- App ----------
app = Flask(__name__)
JWT_SECRET = os.getenv('JWT_SECRET', 'secret')

# ---------- Metrics (Prometheus) ----------
REQUEST_COUNT = Counter(
    'catalog_requests_total',
    'Total HTTP requests to catalog service',
    ['method', 'endpoint', 'status']
)
REQUEST_LATENCY = Histogram(
    'catalog_request_duration_seconds',
    'HTTP request latency',
    ['endpoint']
)


@app.before_request
def _start_timer():
    request._start_time = time.time()


@app.after_request
def _record_metrics(response):
    if hasattr(request, '_start_time'):
        latency = time.time() - request._start_time
        REQUEST_LATENCY.labels(endpoint=request.path).observe(latency)
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=request.path,
        status=response.status_code
    ).inc()
    return response


# ---------- DB ----------
def get_db_connection():
    """Conexiune la baza de date principala (tabletop_db)."""
    conn = psycopg2.connect(
        host=os.getenv('DB_HOST', 'tabletop-db'),
        port=os.getenv('DB_PORT', '5432'),
        database=os.getenv('DB_NAME', 'tabletop_db'),
        user=os.getenv('DB_USER', 'admin'),
        password=os.getenv('DB_PASSWORD', 'boardgamepassword')
    )
    return conn


# ---------- JWT auth decorator ----------
def token_required(f):
    """Decorator pentru rute protejate - valideaza JWT emis de Auth Service."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization')
        if auth_header:
            token = auth_header.split(" ")[1] if " " in auth_header else auth_header
        if not token:
            return jsonify({'message': 'Token is missing!'}), 401
        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            request.token_data = data
        except Exception as e:
            return jsonify({'message': 'Invalid token!', 'error': str(e)}), 401
        return f(*args, **kwargs)
    return decorated


# ============================================================
# PUBLIC ENDPOINTS (no auth needed - browsing the catalog)
# ============================================================

@app.route('/health', methods=['GET'])
def health_check():
    """Health endpoint pentru orchestrator / Kong / monitoring."""
    return jsonify({'status': 'Catalog Service is Up!', 'service': 'catalog'}), 200


@app.route('/metrics', methods=['GET'])
def metrics():
    """Endpoint Prometheus pentru scraping metrici."""
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}


@app.route('/catalog', methods=['GET'])
def list_games():
    """
    Listare jocuri cu filtre optionale via query params:
      ?category=Strategie
      ?difficulty=easy
      ?min_players=2
      ?max_players=6
      ?search=catan
      ?in_stock=true
      ?limit=20&offset=0
    """
    category = request.args.get('category')
    difficulty = request.args.get('difficulty')
    min_players = request.args.get('min_players', type=int)
    max_players = request.args.get('max_players', type=int)
    search = request.args.get('search')
    in_stock = request.args.get('in_stock', '').lower() == 'true'
    limit = min(request.args.get('limit', 50, type=int), 200)
    offset = request.args.get('offset', 0, type=int)

    query = "SELECT gameID, name, description, price, stock, min_players, max_player, difficulty, category FROM games WHERE 1=1"
    params = []

    if category:
        query += " AND category ILIKE %s"
        params.append(category)
    if difficulty:
        query += " AND difficulty ILIKE %s"
        params.append(difficulty)
    if min_players is not None:
        query += " AND (min_players IS NULL OR min_players <= %s)"
        params.append(min_players)
    if max_players is not None:
        query += " AND (max_player IS NULL OR max_player >= %s)"
        params.append(max_players)
    if search:
        query += " AND (name ILIKE %s OR description ILIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])
    if in_stock:
        query += " AND stock > 0"

    query += " ORDER BY name LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params)
        rows = cur.fetchall()
        games = [dict(r) for r in rows]
        # Convert Decimal to float for JSON serialization
        for g in games:
            if g.get('price') is not None:
                g['price'] = float(g['price'])
        logger.info(f"Listed {len(games)} games (filters: category={category}, search={search})")
        return jsonify({'count': len(games), 'games': games}), 200
    except Exception as e:
        logger.error(f"Error listing games: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/catalog/<int:game_id>', methods=['GET'])
def get_game(game_id):
    """Detalii pentru un joc specific."""
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT gameID, name, description, price, stock, min_players, max_player, difficulty, category FROM games WHERE gameID = %s",
            (game_id,)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Game not found'}), 404
        game = dict(row)
        if game.get('price') is not None:
            game['price'] = float(game['price'])
        return jsonify(game), 200
    except Exception as e:
        logger.error(f"Error fetching game {game_id}: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/catalog/categories', methods=['GET'])
def list_categories():
    """Returneaza lista distinct de categorii."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT category FROM games WHERE category IS NOT NULL ORDER BY category")
        categories = [row[0] for row in cur.fetchall()]
        return jsonify({'categories': categories}), 200
    except Exception as e:
        logger.error(f"Error listing categories: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/catalog/<int:game_id>/stock', methods=['GET'])
def check_stock(game_id):
    """Verifica stocul curent pentru un joc - folosit de Order Service."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, stock FROM games WHERE gameID = %s", (game_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Game not found'}), 404
        return jsonify({'gameID': game_id, 'name': row[0], 'stock': row[1], 'available': row[1] > 0}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ============================================================
# PROTECTED ENDPOINTS (admin - require JWT)
# ============================================================

@app.route('/catalog', methods=['POST'])
@token_required
def add_game():
    """Adauga un joc nou in catalog (necesita autentificare)."""
    data = request.get_json() or {}
    required = ['name', 'price']
    if not all(k in data for k in required):
        return jsonify({'error': f'Missing required fields: {required}'}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO games (name, description, price, stock, min_players, max_player, difficulty, category)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING gameID""",
            (
                data['name'],
                data.get('description'),
                data['price'],
                data.get('stock', 0),
                data.get('min_players'),
                data.get('max_players'),
                data.get('difficulty'),
                data.get('category'),
            )
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        username = request.token_data.get('username', 'unknown')
        logger.info(f"User '{username}' added game id={new_id} name='{data['name']}'")
        return jsonify({'message': 'Game added', 'gameID': new_id}), 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Error adding game: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/catalog/<int:game_id>', methods=['PUT'])
@token_required
def update_game(game_id):
    """Modifica un joc existent (necesita autentificare)."""
    data = request.get_json() or {}
    if not data:
        return jsonify({'error': 'No fields to update'}), 400

    allowed = ['name', 'description', 'price', 'stock', 'min_players', 'max_player', 'difficulty', 'category']
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'No valid fields to update'}), 400

    set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
    params = list(updates.values()) + [game_id]

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE games SET {set_clause} WHERE gameID = %s", params)
        if cur.rowcount == 0:
            return jsonify({'error': 'Game not found'}), 404
        conn.commit()
        return jsonify({'message': 'Game updated', 'gameID': game_id}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    logger.info(f"Catalog Service starting on port {port}")
    app.run(host='0.0.0.0', port=port)
