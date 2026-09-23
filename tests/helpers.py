from ambergate.config import default_config


def route(path="/", port=9000, id="root"):
    return {"id": id, "name": "Test", "path": path, "strip_prefix": False,
            "balance": "round_robin", "targets": [{"address": "127.0.0.1", "port": port,
            "weight": 1, "backup": False}], "cache": False, "cache_ttl": 60,
            "websocket": True, "timeout": 5, "body_mb": 0, "rate_rps": 0, "rate_burst": 0}


def config(port=9000):
    c = default_config()
    c["settings"]["rate_rps"] = 0
    c["hosts"] = [{"id": "host1", "domain": "gateway.test", "enabled": True,
                   "routes": [route(port=port)]}]
    return c
