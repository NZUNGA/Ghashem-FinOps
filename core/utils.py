PCT_DISPLAY_CAP = 10000


def format_capped_pct(pct: float) -> str:
    """
    Formats a percentage for display, capping absurdly large values (e.g.
    a spike measured against a near-zero baseline) at ">10,000%" instead
    of printing something like "6305351%".
    """
    if pct > PCT_DISPLAY_CAP:
        return f">{PCT_DISPLAY_CAP:,}%"
    return f"{pct:.0f}%"


def get_client_ip(request):
    """
    Returns the client IP without port number.
    Azure App Service forwards IPs as "host:port" which PostgreSQL
    inet type rejects.
    """
    ip = request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR", "")
    # Take the first IP if comma-separated (proxy chain)
    ip = ip.split(",")[0].strip()
    # Strip port if present (e.g. "209.107.105.245:64299" -> "209.107.105.245")
    if ":" in ip and not ip.startswith("["):
        ip = ip.split(":")[0]
    return ip