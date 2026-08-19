bind = "unix:/run/gunicorn/mahjong.sock"
# socket 0o666 so the nginx user can connect. Safe only because the socket lives
# in a directory shared with nginx alone, inside the container network — nothing
# else can reach the path. Don't carry this setting to a host-mounted socket.
umask = 0
workers = 8          # UvicornWorker (async) on a 6-core box: cores + headroom, since
                     # sync Django views run one-at-a-time per worker (asgiref thread-
                     # sensitive), so blocking work (OCR/Anthropic, DB) needs extra workers.
worker_class = "uvicorn.workers.UvicornWorker"
timeout = 30
keepalive = 5
# Recycle workers only very rarely. Under UvicornWorker a recycle force-closes
# every WebSocket the worker holds (projector screens reconnect + reload), and the
# old value of 1000 tripped often under event load (modal opens, polling, and WS
# upgrades all count toward it). A high value keeps a memory-leak safety net — each
# worker still recycles roughly daily under load, reclaiming any slow creep —
# without the churn. Not 0: that removes the net entirely, and an unbounded leak
# could OOM the box and take redis/postgres (hence the whole site) down with it.
max_requests = 50000
max_requests_jitter = 5000
preload_app = True
