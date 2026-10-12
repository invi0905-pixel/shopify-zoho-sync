#!/usr/bin/env python3
"""
Shopify -> Zoho Books sync (India / GST) for Invi Creation.

Safe by default: runs as a DRY RUN and writes nothing unless you pass --apply.
Zoho Books itself is the source of truth for "what is already synced"
(invoice number = SH-<shopify order number>), so the script is idempotent
and can be re-run or resumed any time.

Usage examples
  python sync.py --days 3                       # dry run, last 3 days
  python sync.py --days 3 --apply               # create contacts + invoices
  python sync.py --days 3 --apply --payments    # also mark Sent + record payments
  python sync.py --all --apply --max-calls 900  # backfill everything (resumes next run)
  python sync.py --export-customers             # customers.csv from Shopify (no writes to Zoho)
  python sync.py --cleanup-report               # items_cleanup.csv (read-only)
"""
import argparse, csv, os, re, sys, time
from datetime import datetime, timedelta, timezone
import requests

# ---------------- config (environment variables / GitHub secrets) ----------------
ZOHO_ACCOUNTS = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")
ZOHO_API = os.getenv("ZOHO_API_URL", "https://www.zohoapis.in/books/v3")
ZOHO_ORG = os.getenv("ZOHO_ORG_ID", "60078008307")              # the FREE org
ZOHO_PAY_ACCOUNT = os.getenv("ZOHO_PAY_ACCOUNT_ID", "3991295000000033173")  # Razorpay Clearing
SHOP = os.getenv("SHOPIFY_STORE")            # e.g. your-store.myshopify.com
SHOP_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN")        # legacy custom app token (shpat_...)
SHOP_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID")      # Dev Dashboard app credentials
SHOP_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET")
SHOP_API_VER = os.getenv("SHOPIFY_API_VERSION", "2025-07")
HOME_STATE = "Tamil Nadu"
DEFAULT_HSN = os.getenv("DEFAULT_HSN", "620412")   # fallback when an item has no HSN
GST_PCT = os.getenv("DEFAULT_GST_PCT", "5")   # confirm with your CA (rate can depend on price)
IST = timezone(timedelta(hours=5, minutes=30))

STATE_CODES = {"Andhra Pradesh":"AP","Arunachal Pradesh":"AR","Assam":"AS","Bihar":"BR","Chhattisgarh":"CT",
 "Goa":"GA","Gujarat":"GJ","Haryana":"HR","Himachal Pradesh":"HP","Jharkhand":"JH","Karnataka":"KA",
 "Kerala":"KL","Madhya Pradesh":"MP","Maharashtra":"MH","Manipur":"MN","Meghalaya":"ML","Mizoram":"MZ",
 "Nagaland":"NL","Odisha":"OR","Punjab":"PB","Rajasthan":"RJ","Sikkim":"SK","Tamil Nadu":"TN",
 "Telangana":"TS","Tripura":"TR","Uttar Pradesh":"UP","Uttarakhand":"UK","West Bengal":"WB",
 "Delhi":"DL","Jammu and Kashmir":"JK","Ladakh":"LA","Puducherry":"PY","Chandigarh":"CH",
 "Andaman and Nicobar Islands":"AN","Dadra and Nagar Haveli and Daman and Diu":"DN","Lakshadweep":"LD"}

class Budget:
    def __init__(self, n): self.left = n
    def use(self):
        if self.left <= 0: raise SystemExit("API call budget reached - run again later (state is kept in Zoho).")
        self.left -= 1
BUDGET = Budget(900)  # Zoho Free = 1000 calls/day

# ---------------- Zoho ----------------
_tok = {"v": None, "exp": 0}
def zoho_token():
    if _tok["v"] and time.time() < _tok["exp"]: return _tok["v"]
    r = requests.post(f"{ZOHO_ACCOUNTS}/oauth/v2/token", data={
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"], "client_id": os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"], "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status(); j = r.json()
    if "access_token" not in j: raise SystemExit(f"Zoho token error: {j}")
    _tok.update(v=j["access_token"], exp=time.time() + 3000)
    return _tok["v"]

def zoho(method, path, params=None, body=None):
    BUDGET.use(); time.sleep(0.7)  # stay under 100 req/min
    p = {"organization_id": ZOHO_ORG, **(params or {})}
    for attempt in range(3):
        r = requests.request(method, f"{ZOHO_API}{path}", params=p, json=body,
                             headers={"Authorization": f"Zoho-oauthtoken {zoho_token()}"}, timeout=60)
        if r.status_code == 429: time.sleep(30); continue
        break
    try: j = r.json()
    except Exception: raise RuntimeError(f"Zoho {r.status_code}: {r.text[:300]}")
    if j.get("code") != 0: raise RuntimeError(f"Zoho {path}: {j.get('code')} {j.get('message')}")
    return j

def zoho_pages(path, key, params=None):
    page = 1
    while True:
        j = zoho("GET", path, {**(params or {}), "page": page, "per_page": 200})
        yield from j.get(key, [])
        if not j.get("page_context", {}).get("has_more_page"): return
        page += 1

# ---------------- Shopify ----------------
def shopify_token():
    global SHOP_TOKEN
    if SHOP_TOKEN: return SHOP_TOKEN
    if not (SHOP_CLIENT_ID and SHOP_CLIENT_SECRET):
        raise SystemExit("Set SHOPIFY_ADMIN_TOKEN, or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET.")
    r = requests.post(f"https://{SHOP}/admin/oauth/access_token", json={
        "client_id": SHOP_CLIENT_ID, "client_secret": SHOP_CLIENT_SECRET, "grant_type": "client_credentials"}, timeout=30)
    if r.status_code != 200: raise SystemExit(f"Shopify token error {r.status_code}: {r.text[:300]}")
    SHOP_TOKEN = r.json()["access_token"]
    return SHOP_TOKEN

def shopify_pages(resource, params):
    url = f"https://{SHOP}/admin/api/{SHOP_API_VER}/{resource}.json"
    while url:
        r = requests.get(url, params=params, headers={"X-Shopify-Access-Token": shopify_token()}, timeout=60)
        r.raise_for_status()
        yield from r.json()[resource]
        nxt = re.search(r'<([^>]+)>; rel="next"', r.headers.get("Link", ""))
        url, params = (nxt.group(1) if nxt else None), None
        time.sleep(0.3)

# ---------------- helpers ----------------
def addr(a):
    a = a or {}
    return {"address": a.get("address1") or "", "street2": a.get("address2") or "", "city": a.get("city") or "",
            "state": a.get("province") or "", "zip": a.get("zip") or "", "country": a.get("country") or "India",
            "phone": a.get("phone") or ""}

def person(c):
    name = (c.get("name") or "").strip() or "Customer"
    first, _, last = name.partition(" ")
    return first, last

def contact_body(order):
    cust = order.get("customer") or {}
    ba, sa = order.get("billing_address") or {}, order.get("shipping_address") or {}
    name = (ba.get("name") or sa.get("name") or f"{cust.get('first_name','')} {cust.get('last_name','')}").strip() or "Customer"
    first, last = person({"name": name})
    state = (sa.get("province") or ba.get("province") or HOME_STATE)
    return {"contact_name": name, "contact_type": "customer", "customer_sub_type": "individual",
            "gst_treatment": "consumer", "place_of_contact": STATE_CODES.get(state, "TN"),
            "billing_address": addr(ba or sa), "shipping_address": addr(sa or ba),
            "contact_persons": [{"first_name": first, "last_name": last, "email": order.get("email") or cust.get("email") or "",
                                 "mobile": (order.get("phone") or cust.get("phone") or sa.get("phone") or ""),
                                 "is_primary_contact": True}]}

def load_items():
    by_sku = {}
    for it in zoho_pages("/items", "items", {"filter_by": "Status.All"}):
        if it.get("sku"): by_sku[it["sku"].strip().lower()] = it
    return by_sku

def load_taxes():
    t = {}
    for x in zoho("GET", "/settings/taxes").get("taxes", []):
        t[x["tax_name"].upper()] = x["tax_id"]
    return t

def unlinked_name(full):
    """Line name that can never equal an existing Zoho item name (Zoho auto-links identical names)."""
    return (full[:88] + " [Shopify]")[:100]

_activated = set()
def ensure_active(it, apply):
    """Zoho refuses invoices for inactive / confirmation_pending items - activate them."""
    if not apply or it.get("status", "active") == "active" or it["item_id"] in _activated: return it
    try:
        zoho("POST", f"/items/{it['item_id']}/active")
        it["status"] = "active"
    except Exception as e:
        if "2115" not in str(e): print(f"  ! could not activate item {it.get('sku')} ({it.get('name')}): {e}")
    _activated.add(it["item_id"])
    return it

def find_item_by_name(name):
    r = zoho("GET", "/items", {"name": name[:100], "filter_by": "Status.All"}).get("items", [])
    for x in r:
        if x["name"].strip().lower() == name[:100].strip().lower(): return x
    return None

def get_item(li, items, taxes, apply):
    """Find the Zoho item by SKU; create it (goods, not stock-tracked) if missing."""
    sku = (li.get("sku") or f"SHP-{li.get('variant_id')}").strip()
    key = sku.lower()
    if key in items: return ensure_active(items[key], apply)
    if not apply:
        print(f"  [dry] would create item {sku}: {li['title']}")
        return {"item_id": "DRY", "sku": sku}
    name = li["title"] + (f" - {li['variant_title']}" if li.get("variant_title") else "")
    prefs = []
    if taxes.get(f"GST{GST_PCT}"): prefs.append({"tax_id": taxes[f"GST{GST_PCT}"], "tax_specification": "intra"})
    if taxes.get(f"IGST{GST_PCT}"): prefs.append({"tax_id": taxes[f"IGST{GST_PCT}"], "tax_specification": "inter"})
    body = {"name": name[:100], "sku": sku, "rate": float(li["price"]), "product_type": "goods",
            "item_type": "sales", "is_taxable": True, "unit": "Nos", "hsn_or_sac": DEFAULT_HSN,
            "description": f"Created from Shopify (variant {li.get('variant_id')})"}
    if prefs: body["item_tax_preferences"] = prefs
    try:
        it = zoho("POST", "/items", body=body)["item"]
    except RuntimeError as e:
        if "already exists" not in str(e): raise
        it = find_item_by_name(name)          # same name, different SKU: reuse that item
        if not it: raise
    items[key] = it
    return ensure_active(it, apply)

def tax_for(item, intra):
    want = "intra" if intra else "inter"
    for p in item.get("item_tax_preferences", []):
        if p.get("tax_specification") == want: return p["tax_id"]
    return item.get("tax_id") or None

def ist_date(iso): return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(IST).strftime("%Y-%m-%d")

def pay_mode(order):
    g = " ".join(order.get("payment_gateway_names") or []).lower()
    if "cod" in g or "cash" in g: return "cash"
    if "razorpay" in g or "upi" in g: return "others"
    return "others"

# ---------------- commands ----------------
def export_customers(path="customers.csv"):
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["shopify_id", "name", "email", "phone", "address", "city", "state", "zip", "orders", "total_spent"])
        for c in shopify_pages("customers", {"limit": 250}):
            a = c.get("default_address") or {}
            w.writerow([c["id"], f"{c.get('first_name') or ''} {c.get('last_name') or ''}".strip(), c.get("email"),
                        c.get("phone") or a.get("phone"), a.get("address1"), a.get("city"), a.get("province"), a.get("zip"),
                        c.get("orders_count"), c.get("total_spent")]); n += 1
    print(f"Wrote {n} customers to {path} (import via Zoho: Sales > Customers > Import).")

def cleanup_report(path="items_cleanup.csv"):
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["item_id", "name", "sku", "status", "problems"])
        for it in zoho_pages("/items", "items", {"filter_by": "Status.All"}):
            pr = []
            if not it.get("hsn_or_sac"): pr.append("missing HSN")
            if it.get("status") == "confirmation_pending": pr.append("confirmation_pending")
            if it.get("unit") == "kg" and it.get("item_type") != "service": pr.append("unit is kg")
            if it.get("item_type") == "sales" and not it.get("track_inventory"): pr.append("not stock tracked")
            if not it.get("sku"): pr.append("missing SKU")
            if pr: w.writerow([it["item_id"], it["name"], it.get("sku"), it.get("status"), "; ".join(pr)]); rows += 1
    print(f"{rows} items need attention -> {path}")

def sync_orders(args):
    since = None if args.all else (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    params = {"status": "any", "financial_status": "paid", "limit": 250, "order": "created_at asc"}
    if since: params["created_at_min"] = since
    orders = [o for o in shopify_pages("orders", params) if not o.get("cancelled_at")]
    print(f"Shopify: {len(orders)} paid, non-cancelled orders in range")

    existing = {}  # invoice_number -> invoice summary
    for inv in zoho_pages("/invoices", "invoices", {"invoice_number_startswith": "SH-"}):
        existing[inv["invoice_number"]] = inv
    items = load_items()
    taxes = load_taxes()
    contacts = {}  # email -> customer_id (lazy lookups)
    stats = dict(created=0, sent=0, paid=0, skipped=0, errors=0)

    for o in orders:
        num = f"SH-{o['order_number']}"
        try:
            inv = existing.get(num)
            was_new = False
            if not inv:
                email = (o.get("email") or "").lower()
                cid = contacts.get(email)
                if not cid and email:
                    r = zoho("GET", "/contacts", {"email": email, "contact_type": "customer"}).get("contacts", [])
                    cid = r[0]["contact_id"] if r else None
                if not cid:
                    if not args.apply: print(f"[dry] would create contact for {num}")
                    else: cid = zoho("POST", "/contacts", body=contact_body(o))["contact"]["contact_id"]
                contacts[email] = cid
                sa = o.get("shipping_address") or o.get("billing_address") or {}
                intra = (sa.get("province") or HOME_STATE) == HOME_STATE
                lines, meta, bad = [], [], False
                for li in o["line_items"]:
                    it = get_item(li, items, taxes, args.apply)
                    meta.append((li["title"] + (f" - {li['variant_title']}" if li.get("variant_title") else ""),
                                 it.get("sku") or li.get("sku")))
                    qty = li["quantity"]; disc = sum(float(d["amount"]) for d in li.get("discount_allocations", []))
                    unit = (float(li["price"]) * qty - disc) / qty
                    line = {"quantity": qty, "rate": round(unit, 2),
                            "tax_id": tax_for(it, intra) or taxes.get(f"{'GST' if intra else 'IGST'}{GST_PCT}"),
                            "hsn_or_sac": it.get("hsn_or_sac") or DEFAULT_HSN}
                    if it.get("status", "active") == "active":
                        line["item_id"] = it["item_id"]
                    elif args.unlinked_fallback:
                        full = li["title"] + (f" - {li['variant_title']}" if li.get("variant_title") else "")
                        line.update(name=unlinked_name(full), description=f"SKU {it.get('sku') or li.get('sku')}", unit="Nos", product_type="goods")
                    else:
                        raise RuntimeError(f"item {it.get('sku')} is pending confirmation in Zoho - confirm it there, or use --unlinked-fallback")
                    lines.append(line)
                if bad: stats["errors"] += 1; continue
                ship = sum(float(s["price"]) for s in o.get("shipping_lines", []))
                body = {"customer_id": cid, "invoice_number": num, "reference_number": f"#{o['order_number']}",
                        "date": ist_date(o["created_at"]), "due_date": ist_date(o["created_at"]),
                        "is_inclusive_tax": True, "gst_treatment": "consumer",
                        "place_of_supply": STATE_CODES.get(sa.get("province") or HOME_STATE, "TN"),
                        "line_items": lines, "notes": f"Shopify order #{o['order_number']}"}
                if ship: body["shipping_charge"] = f"{ship:.2f}"
                if not args.apply: print(f"[dry] would create invoice {num} ({len(lines)} lines, Rs {o['total_price']})"); stats["created"] += 1; continue
                try:
                    inv = zoho("POST", "/invoices", {"ignore_auto_number_generation": "true"}, body)["invoice"]
                except RuntimeError as e:
                    if "2007" in str(e) and args.unlinked_fallback:   # Zoho says an item is inactive: unlink all lines and retry
                        for l, (full, sku) in zip(lines, meta):
                            l.pop("item_id", None); l.update(name=unlinked_name(full), description=f"SKU {sku}", unit="Nos", product_type="goods")
                        inv = zoho("POST", "/invoices", {"ignore_auto_number_generation": "true"}, body)["invoice"]
                    else: raise
                inv["status"] = "draft"; stats["created"] += 1; existing[num] = inv; was_new = True

            if args.payments and inv.get("status") in ("draft", "sent", "overdue", "unpaid") and float(inv.get("balance", 1)) > 0:
                if not args.apply: print(f"[dry] would mark sent + pay {num}"); continue
                if inv["status"] == "draft":
                    zoho("POST", f"/invoices/{inv['invoice_id']}/status/sent"); stats["sent"] += 1
                total = float(inv.get("total") or o["total_price"])
                zoho("POST", "/customerpayments", body={
                    "customer_id": inv["customer_id"], "payment_mode": pay_mode(o), "amount": total,
                    "date": ist_date(o["created_at"]), "reference_number": f"Shopify #{o['order_number']}",
                    "account_id": ZOHO_PAY_ACCOUNT,
                    "invoices": [{"invoice_id": inv["invoice_id"], "amount_applied": total}]})
                stats["paid"] += 1
            elif not was_new: stats["skipped"] += 1
        except SystemExit: raise
        except Exception as e:
            stats["errors"] += 1; print(f"  ! {num}: {e}")
    print(stats, f"| API calls left today ~{BUDGET.left}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2); ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true", help="actually write to Zoho (default: dry run)")
    ap.add_argument("--unlinked-fallback", action="store_true", help="if an item is stuck in confirmation_pending, invoice the line without linking the item")
    ap.add_argument("--payments", action="store_true", help="mark invoices Sent and record payments")
    ap.add_argument("--max-calls", type=int, default=900)
    ap.add_argument("--export-customers", action="store_true"); ap.add_argument("--cleanup-report", action="store_true")
    a = ap.parse_args(); BUDGET.left = a.max_calls
    if a.export_customers: export_customers()
    elif a.cleanup_report: cleanup_report()
    else: sync_orders(a)
