"""Production gateway transport guard; never an enterprise user/role identity."""

import hmac
import ipaddress
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def gateway_denial(request, settings):
    if not settings.production_mode:
        return None
    peer = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        local = False
    token = request.headers.get("x-roc-gateway-token", "")
    if not local or not hmac.compare_digest(
        token.encode(), settings.gateway_shared_secret.get_secret_value().encode()
    ):
        return JSONResponse(
            {"detail": "需要通过获准认证网关访问"}, status_code=403, headers={"Cache-Control": "no-store"}
        )
    public = urlsplit(settings.production_public_origin)
    if request.headers.get("host", "").lower() != public.netloc.lower():
        return JSONResponse({"detail": "网关站点不匹配"}, status_code=403)
    # The fixed configured TLS origin is trusted; arbitrary forwarded headers are not.
    request.scope["scheme"] = "https"
    # Development persistence endpoints carry no trusted enterprise actor.
    if request.method not in ("GET", "HEAD", "OPTIONS") and (
        request.url.path.startswith("/api/v1/operations/workspace")
        or request.url.path.startswith("/api/v1/investigations/")
        and request.url.path.endswith("/feedback")
    ):
        return JSONResponse({"detail": "生产环境尚未开放人工写权限"}, status_code=403)
    return None
