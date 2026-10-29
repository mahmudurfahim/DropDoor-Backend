"""Activity logs. Every call to /api/ is written down: what a reseller does in the app goes to "Reseller logs",
what the admin panel changes goes to "Admin logs" (Django's own log table). Entries older than LOG_DAYS days are deleted automatically.
Passwords, codes and tokens are never stored. ACTIVITY_LOG=0 switches it all off; LOG_READS=0 keeps only the calls that change something."""
import json, os, re, time
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.utils import timezone
from datetime import timedelta

LOG_DAYS = 30
SECRET = re.compile(r"pass|code|token|otp|secret|authorization", re.I)
SKIP = ("/api/media/",)
NAMES = [  # (method, path regex) -> readable action
    ("POST", r"^/api/auth/login/$", "Login"), ("POST", r"^/api/auth/refresh/$", "Session refresh"), ("POST", r"^/api/auth/register/$", "Registration"),
    ("POST", r"^/api/auth/verify/$", "Phone verification"), ("POST", r"^/api/auth/send-code/$", "Code requested"), ("POST", r"^/api/auth/reset/$", "Password reset"),
    ("POST", r"^/api/orders/$", "Order placed"), ("POST", r"^/api/cart/checkout/$", "Order placed from cart"), ("POST", r"^/api/orders/\d+/cancel/$", "Order cancelled"),
    ("POST", r"^/api/withdraws/$", "Withdraw requested"), ("POST", r"^/api/accounts/$", "Payout account added"), ("POST", r"^/api/cart/$", "Added to cart"),
    ("GET", r"^/api/orders/$", "Orders list opened"), ("GET", r"^/api/orders/\d+/$", "Order opened"), ("GET", r"^/api/products/$", "Products opened"),
    ("GET", r"^/api/wallet/$", "Wallet opened"), ("GET", r"^/api/notices/$", "Notices opened"), ("GET", r"^/api/dashboard/$", "Dashboard opened"),
]
_last_purge = [0.0]


def _enabled():
    return os.getenv("ACTIVITY_LOG", "1") != "0"


def _label(method, path):
    for m, rx, name in NAMES:
        if m == method and re.match(rx, path):
            return name
    return f"{method} {path}"[:80]


def _clean(v, depth=0):
    if isinstance(v, dict):
        return {k: ("***" if SECRET.search(str(k)) else _clean(x, depth + 1)) for k, x in list(v.items())[:20]}
    if isinstance(v, list):
        return [_clean(x, depth + 1) for x in v[:10]]
    return v if not isinstance(v, str) else v[:120]


def _detail(method, request, body):
    if body:
        try:
            return json.dumps(_clean(json.loads(body)), ensure_ascii=False)[:300]
        except ValueError:
            return ""
    return request.META.get("QUERY_STRING", "")[:300] if method == "GET" else ""


def purge(days=LOG_DAYS):
    from .models import ResellerLog
    cut = timezone.now() - timedelta(days=days)
    n = ResellerLog.objects.filter(created__lt=cut).delete()[0]
    return n + LogEntry.objects.filter(action_time__lt=cut).delete()[0]


class ActivityLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        body = b""
        watch = _enabled() and path.startswith("/api/") and request.method != "OPTIONS" and not path.startswith(SKIP)
        if watch and request.method in ("POST", "PUT", "PATCH", "DELETE") and "json" in request.META.get("CONTENT_TYPE", "") and int(request.META.get("CONTENT_LENGTH") or 0) < 20000:
            body = request.body  # read before the view: the view still gets it
        response = self.get_response(request)
        if watch:
            try:
                self.record(request, response, body)
            except Exception:
                pass  # logging must never break a request
        return response

    def record(self, request, response, body):
        from .models import ResellerLog
        method, path, user = request.method, request.path, getattr(request, "user", None)
        staff = bool(user and user.is_authenticated and user.is_staff)
        if staff:
            if method == "GET" or path.endswith(("/balance/", "/referrer/")) or re.match(r"^/api/admin/resellers/\d+/$", path):  # (the reseller edits write their own, more detailed entry)
                return
            LogEntry.objects.create(user_id=user.pk, content_type=None, object_id=None, object_repr=_label(method, path)[:200],
                                    action_flag={"POST": CHANGE, "PUT": CHANGE, "PATCH": CHANGE, "DELETE": DELETION}.get(method, CHANGE),
                                    change_message=f"{method} {path} -> {response.status_code}. {_detail(method, request, body)}"[:900])
        else:
            if method == "GET" and os.getenv("LOG_READS", "1") == "0":
                return
            phone = ""
            if user and user.is_authenticated:
                phone = user.phone or user.username
            elif body:
                try:
                    phone = str(json.loads(body).get("username") or json.loads(body).get("phone") or "")[:15]
                except (ValueError, AttributeError):
                    pass
            fwd = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
            ResellerLog.objects.create(user_id=user.pk if user and user.is_authenticated else None, phone=phone, action=_label(method, path), method=method, path=path[:200],
                                       status=response.status_code, detail=_detail(method, request, body), ip=(fwd or request.META.get("REMOTE_ADDR", ""))[:45])
        if time.time() - _last_purge[0] > 6 * 3600:  # old entries are removed about every 6 hours, without any scheduler
            _last_purge[0] = time.time()
            purge()
