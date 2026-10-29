from django.contrib.auth.models import AbstractUser
import re
from urllib.parse import urlsplit
from django.core.exceptions import ValidationError
from django.contrib.admin.models import LogEntry
from django.db import models
from django.db.models import Case, Count, IntegerField, Q, Sum, Value, When
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import storage


def yt_ok(v):
    if not re.match(r"^https?://(www\.|m\.)?(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)[\w-]{6,}", v):
        raise ValidationError("Enter a direct YouTube link")


def video_ok(v):  # any website link is fine (YouTube, Facebook, Google Drive, TikTok...), but only http(s): the app opens it, so javascript:, intent: or file: links are refused
    if not re.match(r"^https?://[^\s]+$", v or "", re.I):
        raise ValidationError("Enter a link that starts with http:// or https://")


def tracking_ok(v):  # only a Steadfast tracking link: the app opens it inside a WebView with JavaScript, so no other site is allowed
    p = urlsplit(v)
    if len(v) > 300 or p.scheme != "https" or p.hostname not in ("steadfast.com.bd", "www.steadfast.com.bd") or p.username or p.password or not p.path.strip("/"):
        raise ValidationError("Paste the Steadfast tracking link (https://steadfast.com.bd/...)")


MAX_DESC_WORDS = 100


def desc_ok(v):  # product description: at most 100 words (and a sane length cap)
    n = len(str(v).split())
    if n > MAX_DESC_WORDS:
        raise ValidationError(f"Description is {n} words. The limit is {MAX_DESC_WORDS} words.")
    if len(v) > 1500:
        raise ValidationError("Description is too long. Keep it under 1500 characters.")


def unset_last(path="position"):
    """Order expression: items that have a priority number (1, 2, 3...) come first, items without one (0) come after them."""
    return Case(When(**{path: 0}, then=Value(1)), default=Value(0), output_field=IntegerField())


def product_id_from_code(q):
    """"DP1008", "dp 1008" (old "EP1008" too) or "1008" -> the product id (8). None when the text is not a product code."""
    m = re.fullmatch(r"(?:DP|EP)?\s*-?\s*(\d{4,9})", str(q or "").strip().upper())
    n = int(m.group(1)) - 1000 if m else 0
    return n if n > 0 else None


def user_from_dropship_id(q):
    """"DDR1001", "ddr 1001" or "1001" -> the reseller with that Dropship ID. None when there is no such reseller (admins have no Dropship ID)."""
    m = re.fullmatch(r"(?:DDR)?\s*-?\s*(\d{4,9})", str(q or "").strip().upper())
    n = int(m.group(1)) - 1000 if m else 0
    return User.objects.filter(pk=n, is_staff=False).first() if n > 0 else None


def options(text):
    """"Black, White" -> ["Black", "White"] (comma or new line separated, no duplicates)."""
    seen, out = set(), []
    for part in re.split(r"[,\n]", text or ""):
        v = part.strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


class User(AbstractUser):  # staff = admin, everyone else = reseller. username = phone
    phone = models.CharField(max_length=11, unique=True, null=True)
    status = models.CharField(max_length=10, default="pending")  # pending / approved / blocked
    phone_verified = models.BooleanField(default=False)
    shop_name = models.CharField(max_length=100, blank=True)  # shop or Facebook page name (a name only, never a link)
    shop_link = models.CharField(max_length=300, blank=True)  # optional: shop address or page link
    address = models.CharField(max_length=300, blank=True)
    referred_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals")  # the reseller whose Dropship ID was given at registration (the admin can change it)

    @property
    def dropship_id(self):  # automatic, e.g. DDR1001: made from the user's number, never typed. Resellers only.
        return f"DDR{1000 + self.id}" if self.id and not self.is_staff else ""

    def __str__(self):
        return f"{self.first_name} ({self.phone or self.username})" if self.first_name else (self.phone or self.username)


class Category(models.Model):  # top-level categories: the first 12 come from a migration, the admin can add, rename, delete them and give them an icon
    name = models.CharField(max_length=60, unique=True)
    position = models.PositiveSmallIntegerField(default=0)
    icon_data = models.BinaryField(null=True, blank=True, editable=False)  # optional icon, same as a sub category's
    icon_ver = models.PositiveIntegerField(default=0)  # +1 on every replace/remove, so the app never shows a cached old icon
    icon_path = models.CharField(max_length=200, blank=True)  # file in Supabase Storage (empty = no icon, or still in icon_data)

    class Meta:
        db_table = "shop_maincategory"
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class SubCategory(models.Model):  # category > sub category > product. The admin creates these.
    name = models.CharField(max_length=60)
    position = models.PositiveIntegerField(default=0)  # priority: smaller number shows first (the admin panel moves it with the arrows)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="subcategories", null=True)  # null only for rows that existed before categories were introduced
    icon_data = models.BinaryField(null=True, blank=True, editable=False)  # optional icon (PNG, keeps its own shape), lives in the database like product photos
    icon_ver = models.PositiveIntegerField(default=0)  # +1 on every replace/remove, so the app never shows a cached old icon
    icon_path = models.CharField(max_length=200, blank=True)  # file in Supabase Storage (empty = the icon is still in icon_data)

    class Meta:
        db_table = "shop_category"  # the old categories table: its rows became sub categories
        ordering = ["position", "name"]
        verbose_name_plural = "sub categories"
        constraints = [models.UniqueConstraint(fields=["category", "name"], name="unique_subcategory_per_category")]

    def __str__(self):
        return f"{self.category.name if self.category_id else '?'} > {self.name}"


class Product(models.Model):
    name = models.CharField(max_length=120)
    subcategory = models.ForeignKey(SubCategory, on_delete=models.PROTECT)
    base_price = models.PositiveIntegerField()  # supplier (admin) price
    in_stock = models.BooleanField(default=True)  # only "in stock / out of stock", no counting
    position = models.PositiveIntegerField(default=0)  # priority inside its sub category: smaller number shows first
    active = models.BooleanField(default=True)
    description = models.TextField(validators=[desc_ok])  # required, max 100 words
    size = models.CharField(max_length=100, blank=True)  # optional, comma separated: each one becomes an option the reseller must choose from, e.g. "M, L, XL"
    color = models.CharField(max_length=100, blank=True)  # optional, comma separated, e.g. "Black, White"
    video_url = models.URLField(max_length=500, blank=True, validators=[video_ok])  # optional: any video link (not only YouTube)

    class Meta:
        ordering = ["position", "-id"]

    @property
    def category(self):
        return self.subcategory.category

    @property
    def code(self):  # automatic product code (DP1001, DP1002, ...): made from the product's number, so it never needs to be typed
        return f"DP{1000 + self.id}" if self.id else ""

    @property
    def size_options(self):
        return options(self.size)

    @property
    def color_options(self):
        return options(self.color)

    def __str__(self):
        return f"{self.code} - {self.name}" if self.id else self.name


class Order(models.Model):
    reseller = models.ForeignKey(User, on_delete=models.PROTECT)
    customer_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=11)
    address = models.CharField(max_length=490)
    note = models.CharField(max_length=300, blank=True)  # optional note the reseller writes when placing the order (e.g. "call before delivery")
    cod = models.PositiveIntegerField()
    zone = models.CharField(max_length=14, default="outside_dhaka")  # inside_dhaka / sub_dhaka / outside_dhaka
    delivery_fee = models.PositiveIntegerField(default=0)  # the charge taken from this order's profit, saved so later price changes never touch it
    profit = models.IntegerField()  # calculated on the server only
    status = models.CharField(max_length=12, default="Pending")  # Pending Confirmed Shipped Delivered Cancelled Returned NeedsReview
    tracking_url = models.URLField(max_length=300, blank=True, validators=[tracking_ok])  # optional Steadfast tracking link, given by the admin when the parcel is shipped
    created = models.DateTimeField(auto_now_add=True)

    @property
    def code(self):
        return f"DO{1000 + self.id}"

    def __str__(self):
        return f"{self.code} - {self.customer_name} ({self.status})"


class RetailCustomer(models.Model):  # the customer a reseller typed into an order: one row is saved for every order
    order = models.OneToOneField(Order, related_name="retail_customer", on_delete=models.CASCADE)
    reseller = models.ForeignKey(User, on_delete=models.PROTECT, related_name="retail_customers")
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=11)
    address = models.CharField(max_length=490)
    status = models.CharField(max_length=12, default="Pending")  # this order's current status (kept in sync automatically)
    total_delivered = models.PositiveIntegerField(default=0)  # how many orders to this phone number are Delivered (all resellers)
    total_returned = models.PositiveIntegerField(default=0)  # how many orders to this phone number are Returned
    delivered_cod = models.PositiveIntegerField(default=0)  # COD amount (taka) of those delivered orders
    returned_cod = models.PositiveIntegerField(default=0)  # COD amount (taka) of those returned orders
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "shop_retailcustomer"
        verbose_name_plural = "retail customers"
        ordering = ["-id"]

    def __str__(self):
        return f"{self.name} ({self.phone})"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, null=True, on_delete=models.SET_NULL)  # a deleted product leaves the order intact
    name = models.CharField(max_length=120, default="")  # snapshot, so the order still reads well after the product is deleted or renamed
    category = models.CharField(max_length=60, blank=True)
    subcategory = models.CharField(max_length=60, blank=True)
    size = models.CharField(max_length=100, blank=True)  # the option the reseller chose (blank when the product has none)
    color = models.CharField(max_length=100, blank=True)
    qty = models.PositiveIntegerField()
    base_price = models.PositiveIntegerField()
    sell_price = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.name} x{self.qty}"


class Ledger(models.Model):  # balance = sum(amount). Never edit a balance directly.
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    amount = models.IntegerField()
    kind = models.CharField(max_length=12)  # profit / withdraw / refund / return_fee (a returned order's delivery charge, can push the balance below zero) / referral (bonus for the referrer when a referred reseller's order is delivered) / adjust (admin edit)
    ref = models.CharField(max_length=20, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:  # database-level guards: an order's profit is credited once, and its return fee is charged once
        constraints = [models.UniqueConstraint(fields=["kind", "ref"], condition=Q(kind="profit"), name="one_profit_per_order"),
                       models.UniqueConstraint(fields=["kind", "ref"], condition=Q(kind="return_fee"), name="one_return_fee_per_order"),
                       models.UniqueConstraint(fields=["kind", "ref"], condition=Q(kind="referral"), name="one_referral_per_order")]

    def __str__(self):
        return f"{self.user} {self.amount:+d} ({self.kind})"


class Withdraw(models.Model):
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    amount = models.PositiveIntegerField()
    account = models.CharField(max_length=60)
    balance_before = models.IntegerField(null=True, blank=True)  # the reseller's ledger balance at the moment of the request (before the amount was deducted)
    status = models.CharField(max_length=10, default="Pending")  # Pending / Paid / Rejected
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} - ৳{self.amount} ({self.status})"


class Setting(models.Model):  # admin-editable numbers: delivery_inside, delivery_sub, delivery_outside, min_withdraw, hold_days, referral_bonus
    key = models.CharField(max_length=30, unique=True)
    value = models.IntegerField()

    def __str__(self):
        return f"{self.key} = {self.value}"


class Otp(models.Model):
    phone = models.CharField(max_length=11)
    purpose = models.CharField(max_length=10)  # register / reset
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    used = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.phone} ({self.purpose})"


class ProductImage(models.Model):  # photos live in Supabase Storage (path); old rows may still hold the bytes in data until migrated
    product = models.ForeignKey(Product, related_name="images", on_delete=models.CASCADE)
    data = models.BinaryField(editable=False, null=True, blank=True)
    path = models.CharField(max_length=200, blank=True)  # file in Supabase Storage (empty = the photo is still in data)
    ctype = models.CharField(max_length=30, default="image/jpeg")
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    @property
    def storage_url(self):
        return storage.public_url(storage.PHOTO, self.path)

    def __str__(self):
        return f"{self.product} - photo {self.position + 1}"


@receiver(post_delete, sender=ProductImage)
def _remove_stored_photo(sender, instance, **kwargs):  # photo deleted, or its product deleted: remove the file too
    storage.delete(storage.PHOTO, instance.path)


class Favorite(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)

    class Meta:
        unique_together = [("user", "product")]

    def __str__(self):
        return f"{self.user} likes {self.product}"


class CartItem(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    size = models.CharField(max_length=100, blank=True)
    color = models.CharField(max_length=100, blank=True)
    qty = models.PositiveIntegerField()
    sell_price = models.PositiveIntegerField()

    class Meta:  # the same product in another size or color is a separate cart line
        constraints = [models.UniqueConstraint(fields=["user", "product", "size", "color"], name="one_cart_line_per_variant")]

    def __str__(self):
        return f"{self.user} - {self.product} x{self.qty}"


class OrderNote(models.Model):  # admin notes shown on the reseller's order tracking
    order = models.ForeignKey(Order, related_name="notes", on_delete=models.CASCADE)
    status = models.CharField(max_length=12, blank=True)
    text = models.CharField(max_length=300)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.order}: {self.text[:40]}"


class PayoutAccount(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    kind = models.CharField(max_length=8)  # bkash / nagad / bank
    number = models.CharField(max_length=40)
    holder = models.CharField(max_length=80, blank=True)
    bank_name = models.CharField(max_length=80, blank=True)
    branch = models.CharField(max_length=80, blank=True)

    @property
    def label(self):
        return f"Bank: {self.bank_name} {self.number}" if self.kind == "bank" else f"{self.kind.title()} {self.number}"

    def __str__(self):
        return f"{self.user} - {self.label}"


class Notice(models.Model):  # announcements shown to resellers in the app's Notice tab
    title = models.CharField(max_length=120)
    body = models.TextField(max_length=2000, blank=True)
    pinned = models.BooleanField(default=False)  # pinned notices stay on top
    active = models.BooleanField(default=True)  # inactive = hidden from the app
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pinned", "-id"]

    def __str__(self):
        return self.title


class ResellerLog(models.Model):  # everything a reseller does in the app (also failed logins). Entries older than 30 days are deleted automatically.
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    phone = models.CharField(max_length=15, blank=True)
    action = models.CharField(max_length=80)
    method = models.CharField(max_length=7)
    path = models.CharField(max_length=200)
    status = models.PositiveSmallIntegerField()
    detail = models.CharField(max_length=300, blank=True)  # what was sent (never passwords, codes or tokens)
    ip = models.CharField(max_length=45, blank=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "reseller log"
        verbose_name_plural = "reseller logs"

    def __str__(self):
        return f"{self.phone or '-'}: {self.action} ({self.status})"


class AdminAccount(User):  # the people who can log in to the admin panel (staff users); added in Django admin > Admin accounts
    class Meta:
        proxy = True
        verbose_name = "admin account"
        verbose_name_plural = "admin accounts"


class AdminLog(LogEntry):  # Django's own history of what the admin changed, shown under a clearer name
    class Meta:
        proxy = True
        verbose_name = "admin log"
        verbose_name_plural = "admin logs"


# ---------- automatic bookkeeping ----------
def refresh_retail(phone):
    """Keeps the retail customers table up to date: each row shows its order's status, and the delivered / returned totals of that phone number."""
    rows = list(RetailCustomer.objects.filter(phone=phone).select_related("order"))
    if not rows:
        return
    tot = {}
    for st in ("Delivered", "Returned"):
        a = Order.objects.filter(phone=phone, status=st).aggregate(n=Count("id"), s=Sum("cod"))
        tot[st] = (a["n"] or 0, a["s"] or 0)
    for rc in rows:
        rc.status = rc.order.status
        rc.total_delivered, rc.delivered_cod = tot["Delivered"]
        rc.total_returned, rc.returned_cod = tot["Returned"]
    RetailCustomer.objects.bulk_update(rows, ["status", "total_delivered", "total_returned", "delivered_cod", "returned_cod"])


@receiver(post_save, sender=Order)
def _order_saved(sender, instance, update_fields=None, **kwargs):
    if update_fields is not None and "status" not in update_fields:
        return
    refresh_retail(instance.phone)


@receiver(post_save, sender=RetailCustomer)
def _retail_saved(sender, instance, created, **kwargs):
    if created:
        refresh_retail(instance.phone)
