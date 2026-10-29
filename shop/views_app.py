import re
"""Reseller-app endpoints: signup, products with photos/video, favorites, cart, orders, accounts, withdraw, dashboard."""
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from shipping.sms import SMSError
from . import otp, storage
from .models import product_id_from_code, user_from_dropship_id, Category, SubCategory, CartItem, Favorite, Order, OrderItem, OrderNote, PayoutAccount, Product, ProductImage, RetailCustomer, User, Ledger, Withdraw, unset_last
from .services import AUTO_NOTES, OPEN, ZONES, available, cfg, delivery_fee, fees, pending, set_status
from .views import LINK, ORDER_STATES, PHONE, Approved, err, oj




@api_view(["POST"])
@permission_classes([AllowAny])
def register(r):
    d = r.data
    g = lambda k: str(d.get(k, "")).strip()
    name, phone, shop, addr, pw = g("name"), g("phone"), g("shop_name"), g("address"), str(d.get("password", ""))
    link = g("shop_link")  # optional: shop address or page link
    if not (name and shop and addr) or not PHONE.fullmatch(phone) or len(pw) < 6:
        return err("Name, 11-digit phone, shop/page name, address and a password of 6+ characters are required")
    if LINK.search(shop):
        return err("Shop/page name cannot be a link. Write only the name, and put the link in the shop address / page link field.")
    if len(link) > 300:
        return err("The shop address / page link is too long")
    if User.objects.filter(phone=phone).exists():
        return err("This phone number is already registered", 409)
    referrer = None
    if g("referral"):  # optional: the Dropship ID (DDR1001) of the reseller who invited this person
        referrer = user_from_dropship_id(g("referral"))
        if not referrer:
            return err("Dropship ID not found. Check the ID or leave it empty.")
    User.objects.create_user(username=phone, phone=phone, first_name=name, shop_name=shop[:100], shop_link=link, address=addr[:300], password=pw, referred_by=referrer)
    try:
        otp.issue(phone, "register")
    except (ValueError, SMSError) as e:
        print(f"[register SMS failed] {phone}: {e}")  # visible in Render logs; the person can ask for a new code
    return Response({"message": "Registered. Enter the SMS code, then wait for admin approval."}, status=201)


# ---------- products ----------
def pj(p, r, favs=()):
    sub = p.subcategory
    return {"id": p.id, "code": p.code, "name": p.name, "category": sub.category_id, "category_name": sub.category.name if sub.category_id else "",
            "subcategory": sub.id, "subcategory_name": sub.name, "base_price": p.base_price,
            "in_stock": p.in_stock, "description": p.description, "size": p.size, "color": p.color,
            "sizes": p.size_options, "colors": p.color_options,  # the options a reseller must choose from (empty = nothing to choose)
            "video_url": p.video_url, "is_favorite": p.id in favs,
            "images": [i.storage_url or r.build_absolute_uri(f"/api/media/product/{i.id}/") for i in p.images.all()]}


def _pq():  # only active products that have at least 1 photo are visible to resellers, in the admin's priority order
    return (Product.objects.filter(active=True).select_related("subcategory__category").prefetch_related("images")
            .annotate(n=Count("images")).filter(n__gte=1).order_by("subcategory__category__position", unset_last("subcategory__position"), "subcategory__position", unset_last(), "position", "-id"))


def check_variant(p, size, color):
    """The reseller must pick one of the product's sizes and one of its colors (when the admin gave any). Returns the exact option names."""
    out = []
    for label, val, opts in (("size", size, p.size_options), ("color", color, p.color_options)):
        val = str(val or "").strip()
        if not opts:
            out.append("")  # nothing to choose for this product
            continue
        hit = next((o for o in opts if o.lower() == val.lower()), None)
        if not hit:
            raise ValueError(f"{p.name}: choose a {label} ({', '.join(opts)})")
        out.append(hit)
    return out[0], out[1]


def _favs(u):
    return set(Favorite.objects.filter(user=u).values_list("product_id", flat=True))


@api_view(["GET"])
@permission_classes([Approved])
def products(r):
    qs = _pq()
    if r.GET.get("category", "").isdigit():
        qs = qs.filter(subcategory__category_id=r.GET["category"])
    if r.GET.get("subcategory", "").isdigit():
        qs = qs.filter(subcategory_id=r.GET["subcategory"])
    if r.GET.get("q"):
        q = r.GET["q"].strip()  # product name, or the product code (DP1008 / 1008)
        pid = product_id_from_code(q)
        qs = qs.filter(Q(name__icontains=q) | Q(id=pid)) if pid else qs.filter(name__icontains=q)
    f = _favs(r.user)
    return Response([pj(p, r, f) for p in qs])


@api_view(["GET"])
@permission_classes([Approved])
def product_detail(r, pk):
    p = _pq().filter(pk=pk).first()
    return Response(pj(p, r, _favs(r.user))) if p else err("Product not found", 404)


def media(request, pk):  # plain Django view: public, cached, not rate limited (app image loaders send no token)
    i = ProductImage.objects.filter(pk=pk).first()
    if not i or not (i.path or i.data):
        raise Http404
    if i.path:  # already in Supabase: old links keep working by redirecting there
        resp = HttpResponseRedirect(i.storage_url)
        resp["Cache-Control"] = "public, max-age=86400"
        return resp
    resp = HttpResponse(bytes(i.data), content_type=i.ctype)
    resp["Cache-Control"] = "public, max-age=86400"
    return resp


def subcategory_media(request, pk):  # public + cached (the ?v= number in the URL changes when the icon is replaced)
    return _icon_media(SubCategory, pk)


def category_media(request, pk):
    return _icon_media(Category, pk)


def _icon_media(model, pk):
    s = model.objects.filter(pk=pk).only("icon_data", "icon_path").first()
    if not s or not (s.icon_path or s.icon_data):
        raise Http404
    if s.icon_path:
        resp = HttpResponseRedirect(storage.public_url(storage.ICON, s.icon_path))
        resp["Cache-Control"] = "public, max-age=86400"
        return resp
    resp = HttpResponse(bytes(s.icon_data), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@api_view(["GET"])
@permission_classes([Approved])
def favorites(r):
    ids = _favs(r.user)
    return Response([pj(p, r, ids) for p in _pq().filter(id__in=ids)])


@api_view(["POST"])
@permission_classes([Approved])
def favorite_toggle(r, pk):
    if not Product.objects.filter(pk=pk, active=True).exists():
        return err("Product not found", 404)
    f, made = Favorite.objects.get_or_create(user=r.user, product_id=pk)
    if not made:
        f.delete()
    return Response({"favorite": made})


# ---------- orders ----------
def make_order(user, d, items):
    name, phone, addr = str(d.get("customer_name", "")).strip(), str(d.get("phone", "")), str(d.get("address", "")).strip()
    if not items or not name or not addr or not PHONE.fullmatch(phone):
        raise ValueError("Enter the customer's name, address, 11-digit phone and at least one item")
    note = str(d.get("note", "") or "").strip()[:300]  # optional
    zone = str(d.get("zone", ""))
    fee = delivery_fee(zone)  # raises a clear error when the area is missing or unknown
    try:
        with transaction.atomic():
            lines = []
            for it in items:
                p = Product.objects.select_related("subcategory__category").get(pk=it["product"], active=True)
                q, s = int(it["qty"]), int(it["sell_price"])
                if q < 1 or q > 100 or s < p.base_price:
                    raise ValueError(f"{p.name}: selling price must be at least ৳{p.base_price}" if s < p.base_price else f"{p.name}: quantity must be between 1 and 100")
                if not p.in_stock:
                    raise ValueError(f"{p.name} is out of stock")
                size, color = check_variant(p, it.get("size"), it.get("color"))
                lines.append((p, q, s, size, color))
            profit = sum((s - p.base_price) * q for p, q, s, _, _ in lines)  # the delivery charge is added to the customer's price, so it is not taken from the profit
            if profit <= 0:
                raise ValueError("Your extra profit must be more than zero. Raise the selling price.")
            o = Order.objects.create(reseller=user, customer_name=name, phone=phone, address=addr, note=note, profit=profit, zone=zone, delivery_fee=fee,
                                     cod=sum(s * q for _, q, s, _, _ in lines) + fee)  # what the customer pays: products + delivery charge
            RetailCustomer.objects.create(order=o, reseller=user, name=name[:100], phone=phone, address=addr[:490])  # every order keeps its customer in the retail customers table
            for p, q, s, size, color in lines:
                sc = p.subcategory
                OrderItem.objects.create(order=o, product=p, name=p.name, category=sc.category.name if sc.category_id else "", subcategory=sc.name,
                                         size=size, color=color, qty=q, base_price=p.base_price, sell_price=s)
            OrderNote.objects.create(order=o, status="Pending", text=AUTO_NOTES["Pending"])  # automatic note: "Your order is pending."
    except (KeyError, TypeError, Product.DoesNotExist):
        raise ValueError("One of the items is invalid or no longer available")
    return o


@api_view(["GET", "POST"])
@permission_classes([Approved])
def orders(r):
    if r.method == "GET":
        qs = Order.objects.filter(reseller=r.user).prefetch_related("items", "notes").order_by("-id")
        if r.GET.get("status"):
            qs = qs.filter(status=r.GET["status"])
        return Response([oj(o) for o in qs])
    try:
        o = make_order(r.user, r.data, r.data.get("items") or [])
    except ValueError as e:
        return err(str(e))
    return Response(oj(o), status=201)


@api_view(["GET"])
@permission_classes([Approved])
def order_detail(r, pk):
    o = Order.objects.filter(pk=pk, reseller=r.user).first()
    return Response(oj(o)) if o else err("Order not found", 404)


@api_view(["POST"])
@permission_classes([Approved])
def order_cancel(r, pk):
    with transaction.atomic():
        o = Order.objects.select_for_update().filter(pk=pk, reseller=r.user).first()
        if not o:
            return err("Order not found", 404)
        if o.status != "Pending":
            return err(f"Only pending orders can be cancelled. This order is {o.status}.")
        o = set_status(o, "Cancelled", note=False)  # the reseller's own note below replaces the automatic one
        OrderNote.objects.create(order=o, status="Cancelled", text="Cancelled by reseller")
    return Response(oj(o))


@api_view(["POST"])
@permission_classes([IsAdminUser])
def admin_note(r, pk):  # a note can be added or updated at any status
    o, text = Order.objects.filter(pk=pk).first(), str(r.data.get("text", "")).strip()[:300]
    if not o or not text:
        return err("Order not found or the note is empty", 404 if not o else 400)
    OrderNote.objects.create(order=o, status=o.status, text=text)
    return Response(oj(o, admin=True))


# ---------- cart ----------
def _cart(r, zone=""):
    rows = CartItem.objects.filter(user=r.user).select_related("product__subcategory__category").prefetch_related("product__images").order_by("id")
    items = [{"id": c.id, "product": pj(c.product, r), "size": c.size, "color": c.color, "qty": c.qty, "sell_price": c.sell_price,
              "extra_profit": (c.sell_price - c.product.base_price) * c.qty} for c in rows]
    extra, fee = sum(i["extra_profit"] for i in items), (cfg(ZONES[zone]) if zone in ZONES else None)  # fee/profit stay empty until the area is chosen
    return {"items": items, "extra_profit": extra, "zone": zone if zone in ZONES else None, "delivery_charge": fee,
            "delivery_charges": fees(), "profit": extra,
            "products_total": sum(c.sell_price * c.qty for c in rows), "cod_total": sum(c.sell_price * c.qty for c in rows) + (fee or 0)}  # cod_total = products + the delivery charge of the chosen area


@api_view(["GET", "POST"])
@permission_classes([Approved])
def cart(r):
    if r.method == "POST":
        try:
            p = Product.objects.get(pk=r.data["product"], active=True)
            q, s = int(r.data["qty"]), int(r.data["sell_price"])
        except (KeyError, TypeError, ValueError, Product.DoesNotExist):
            return err("Send a valid product, qty and sell_price")
        if q < 1 or q > 100:
            return err("Quantity must be between 1 and 100")
        if not p.in_stock:
            return err(f"{p.name} is out of stock")
        if s < p.base_price:
            return err(f"Selling price must be at least ৳{p.base_price}")
        try:
            size, color = check_variant(p, r.data.get("size"), r.data.get("color"))
        except ValueError as e:
            return err(str(e))
        CartItem.objects.update_or_create(user=r.user, product=p, size=size, color=color, defaults={"qty": q, "sell_price": s})
    return Response(_cart(r, str(r.GET.get("zone", ""))))  # GET /cart/?zone=inside_dhaka shows the profit for that area


@api_view(["DELETE"])
@permission_classes([Approved])
def cart_remove(r, pk):
    CartItem.objects.filter(user=r.user, pk=pk).delete()
    return Response(_cart(r, str(r.GET.get("zone", ""))))


@api_view(["POST"])
@permission_classes([Approved])
def cart_checkout(r):
    items = [{"product": c.product_id, "qty": c.qty, "sell_price": c.sell_price, "size": c.size, "color": c.color} for c in CartItem.objects.filter(user=r.user)]
    try:
        o = make_order(r.user, r.data, items)
    except ValueError as e:
        return err(str(e))
    CartItem.objects.filter(user=r.user).delete()
    return Response(oj(o), status=201)


# ---------- payout accounts & withdraw ----------
def aj(a):
    return {"id": a.id, "kind": a.kind, "label": a.label, "number": a.number, "holder": a.holder, "bank_name": a.bank_name, "branch": a.branch}


@api_view(["GET", "POST"])
@permission_classes([Approved])
def accounts(r):
    if r.method == "POST":
        d = r.data
        kind, num, holder, bank = d.get("kind"), str(d.get("number", "")).strip(), str(d.get("holder", "")).strip(), str(d.get("bank_name", "")).strip()
        if kind in ("bkash", "nagad"):
            if not PHONE.fullmatch(num):
                return err("Enter an 11-digit mobile number")
        elif kind == "bank":
            if not (num and holder and bank):
                return err("Bank name, account holder and account number are required")
        else:
            return err("kind must be bkash, nagad or bank")
        if PayoutAccount.objects.filter(user=r.user).count() >= 5:
            return err("You can save up to 5 accounts")
        a = PayoutAccount.objects.create(user=r.user, kind=kind, number=num[:40], holder=holder[:80], bank_name=bank[:80], branch=str(d.get("branch", ""))[:80])
        return Response(aj(a), status=201)
    return Response([aj(a) for a in PayoutAccount.objects.filter(user=r.user).order_by("id")])


@api_view(["DELETE"])
@permission_classes([Approved])
def account_delete(r, pk):
    PayoutAccount.objects.filter(user=r.user, pk=pk).delete()
    return Response(status=204)


@api_view(["POST"])
@permission_classes([Approved])
def withdraw_create(r):
    try:
        amt = int(r.data.get("amount"))
    except (TypeError, ValueError):
        return err("Enter an amount")
    aid = str(r.data.get("account_id", ""))
    acc = PayoutAccount.objects.filter(user=r.user, pk=aid).first() if aid.isdigit() else None
    if amt < cfg("min_withdraw"):
        return err(f"Minimum withdraw is ৳{cfg('min_withdraw')}")
    if not acc:
        return err("Choose one of your saved accounts (account_id)")
    with transaction.atomic():
        User.objects.select_for_update().get(pk=r.user.pk)  # serialize concurrent requests
        if Withdraw.objects.filter(user=r.user, status="Pending").exists():
            return err("You already have a pending withdraw request. Wait until it is paid or rejected.", 409)
        if amt > available(r.user):
            return err("Amount is more than your available balance")
        before = Ledger.objects.filter(user=r.user).aggregate(s=Sum("amount"))["s"] or 0
        w = Withdraw.objects.create(user=r.user, amount=amt, account=acc.label[:60], balance_before=before)
        Ledger.objects.create(user=r.user, amount=-amt, kind="withdraw", ref=str(w.id))
    return Response({"id": w.id, "status": w.status}, status=201)


# ---------- reseller dashboard ----------
@api_view(["GET"])
@permission_classes([Approved])
def dashboard(r):
    u, qs = r.user, Order.objects.filter(reseller=r.user)
    cnt = {s: qs.filter(status=s).count() for s in ORDER_STATES}
    done = qs.filter(status="Delivered").aggregate(profit=Sum("profit"), sales=Sum("cod"))
    paid = Q(status="Delivered")
    rows = (qs.annotate(m=TruncMonth("created")).values("m")
            .annotate(orders=Count("id"), delivered=Count("id", filter=paid), profit=Sum("profit", filter=paid), sales=Sum("cod", filter=paid))
            .order_by("-m")[:12])
    return Response({"orders": cnt, "total_orders": qs.count(), "completed_orders": cnt["Delivered"],
                     "total_profit": done["profit"] or 0, "total_sales": done["sales"] or 0,
                     "available": available(u), "pending_profit": pending(u),
                     "withdrawn": Withdraw.objects.filter(user=u, status="Paid").aggregate(s=Sum("amount"))["s"] or 0,
                     "referred_count": User.objects.filter(referred_by=u).count(),
                     "referral_earned": Ledger.objects.filter(user=u, kind="referral").aggregate(s=Sum("amount"))["s"] or 0,
                     "monthly": [{"month": x["m"].strftime("%Y-%m"), "orders": x["orders"], "delivered": x["delivered"],
                                  "profit": x["profit"] or 0, "sales": x["sales"] or 0} for x in rows]})


@api_view(["GET"])
@permission_classes([Approved])
def referrals(r):
    """The reseller's referral report: who joined with their Dropship ID, how many orders each delivered, and the bonus earned from each (one level only)."""
    u = r.user
    people = list(User.objects.filter(referred_by=u).order_by("-id"))
    rows = list(Ledger.objects.filter(user=u, kind="referral").order_by("-id"))
    ids = [int(x.ref[2:]) - 1000 for x in rows if x.ref[2:].isdigit()]  # a bonus row's ref is the order code (DO1008)
    orders = {o.id: o for o in Order.objects.filter(id__in=ids).select_related("reseller")}
    earned, history, months = {}, [], {}
    for x in rows:
        o = orders.get(int(x.ref[2:]) - 1000) if x.ref[2:].isdigit() else None
        if o:
            earned[o.reseller_id] = earned.get(o.reseller_id, 0) + x.amount
        m = timezone.localtime(x.created).strftime("%Y-%m")
        months[m] = months.get(m, [0, 0]); months[m][0] += x.amount; months[m][1] += 1
        if len(history) < 30:
            history.append({"code": x.ref, "name": o.reseller.first_name if o else "", "amount": x.amount, "created": x.created})
    stat = {x["reseller_id"]: x for x in Order.objects.filter(reseller__referred_by=u).values("reseller_id").annotate(
        delivered=Count("id", filter=Q(status="Delivered")), open=Count("id", filter=Q(status__in=OPEN)))}
    out = []
    for p in people:
        st = stat.get(p.id, {})
        out.append({"id": p.id, "name": p.first_name, "shop": p.shop_name, "phone": (p.phone[:3] + "XXXXX" + p.phone[-3:]) if p.phone else "", "status": p.status, "joined": p.date_joined,
                    "delivered": st.get("delivered", 0), "open": st.get("open", 0), "earned": earned.get(p.id, 0)})
    this = timezone.localtime().strftime("%Y-%m")
    bonus = cfg("referral_bonus")
    return Response({"dropship_id": u.dropship_id, "bonus": bonus, "total_referred": len(people), "active_referred": sum(1 for x in out if x["delivered"] > 0),
                     "total_earned": sum(x.amount for x in rows), "bonus_orders": len(rows), "this_month": months.get(this, [0, 0])[0],
                     "open_orders": sum(x["open"] for x in out), "potential": sum(x["open"] for x in out) * bonus,  # bonus still to come if every open order of the referred resellers is delivered
                     "people": out, "history": history,
                     "monthly": [{"month": k, "amount": v[0], "orders": v[1]} for k, v in sorted(months.items(), reverse=True)[:12]]})
