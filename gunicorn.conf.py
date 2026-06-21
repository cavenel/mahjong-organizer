bind = "unix:/run/gunicorn/mahjong.sock"
umask = 0            # socket 0o666 so the nginx user can connect
workers = 8          # UvicornWorker (async) on a 6-core box: cores + headroom, since
                     # sync Django views run one-at-a-time per worker (asgiref thread-
                     # sensitive), so blocking work (OCR/Anthropic, DB) needs extra workers.
worker_class = "uvicorn.workers.UvicornWorker"
timeout = 30
keepalive = 5
max_requests = 1000    # recycle workers periodically to avoid memory creep
max_requests_jitter = 100
preload_app = True
