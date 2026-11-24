"""Pure calculation, no model: what the basket costs in each store, and whether splitting pays off."""
import re
from itertools import combinations


# an alternative may be up to 2.5 times bigger or smaller than the chosen product (12 eggs -> 30, 400 g -> 1 kg).
# Stores are compared for the same amount (comparable_prices), so a different size does not distort the comparison.
MAX_SIZE_RATIO = 2.5
UNITS = {"גרם": 1, "קילוגרם": 1000, 'ק"ג': 1000, "מיליליטר": 1, 'מ"ל': 1, "מ'ל": 1, "ליטר": 1000}


def size_of(quantity: str, unit: str, name: str = "") -> float | None:
    """Package size in grams or ml (as the chains publish it), or the number of units, or None when unknown.
    For products sold by units the chains write quantity 1, and the count is only in the name:
    "ביצים ארוזות 18יח L", "ביצים M 30 יחידות"."""
    if unit and unit.strip() in ("יחידות", "יח'", "יח"):
        count = re.search(r"(\d+)\s*יח", name or "")
        return float(count.group(1)) if count else None
    try:
        size = float(quantity) * UNITS[unit.strip()]
    except (ValueError, KeyError, AttributeError):
        return None
    return size or None


def store_options(prices: dict[tuple, dict[str, float]], options: dict[str, list[str]],
                  sizes: dict[str, float | None], cheapest: set[str] = frozenset()):
    """Each product may be replaced by equally good alternatives (options = {chosen: [chosen, *alternatives]}).
    A store that sells the chosen product keeps it. A store that lacks it gets the alternative closest in size
    (at most MAX_SIZE_RATIO away), then the cheapest: the cheapest alone would be the smallest package
    (250 g of honey instead of 400 g), the cheapest per kilo the largest one.
    Products in `cheapest` (the user asked for the cheapest) take the lowest price per kilo / liter in every store;
    options of unknown size come after the ones with a known size, by price.
    Returns (prices keyed by the chosen codes, {store: {chosen: the code really bought there}})."""
    merged, used = {}, {}
    for store, store_prices in prices.items():
        for chosen, codes in options.items():
            if chosen in cheapest:
                available = [c for c in codes if c in store_prices]
                if not available:
                    continue
                per_kilo = lambda c: store_prices[c] / sizes[c] if sizes.get(c) else float("inf")  # noqa: E731
                code = min(available, key=lambda c: (per_kilo(c), store_prices[c]))
            elif chosen in store_prices:
                code = chosen
            else:
                # the closest size (or an unknown one), not wildly different: 100 g is no substitute for 1 kg
                target = sizes.get(chosen)
                ratio = lambda c: max(sizes[c], target) / min(sizes[c], target) if target and sizes.get(c) else None  # noqa: E731
                available = [c for c in codes if c in store_prices and (ratio(c) is None or ratio(c) <= MAX_SIZE_RATIO)]
                if not available:
                    continue
                code = min(available, key=lambda c: (ratio(c) or MAX_SIZE_RATIO, store_prices[c]))
            merged.setdefault(store, {})[chosen] = store_prices[code]
            used.setdefault(store, {})[chosen] = code
    return merged, used


def comparable_prices(prices: dict[tuple, dict[str, float]], used: dict, sizes: dict[str, float | None]):
    """Prices for comparing stores: each product's price scaled to the size of the chosen product, so 350 g in one
    store and 1 kg in another are compared for the same amount. The chosen product's size is the amount the user
    wrote ("דבש 1 ק"ג" -> the model picks 1 kg), or, when they wrote no amount, the usual package the model picked.
    When a size is unknown (products sold by units), the shelf price is used as is."""
    compare = {}
    for store, store_prices in prices.items():
        for chosen, price in store_prices.items():
            bought, target = used.get(store, {}).get(chosen, chosen), sizes.get(chosen)
            size = sizes.get(bought)
            compare.setdefault(store, {})[chosen] = price * target / size if target and size else price
    return compare


def store_totals(prices: dict[tuple, dict[str, float]], wanted: dict[str, int], compare: dict | None = None) -> list[dict]:
    """For every store: what the basket costs at the register (total), the same basket for comparable amounts
    (compare, see comparable_prices), and which products are missing.
    Sorted: fewest missing products first, then cheapest by compare."""
    compare = compare or prices
    totals = []
    for store, store_prices in prices.items():
        sold = [code for code in wanted if code in store_prices]
        totals.append({
            "store": store,
            "total": round(sum(store_prices[c] * wanted[c] for c in sold), 2),
            "compare": round(sum(compare[store][c] * wanted[c] for c in sold), 2),
            "missing": [code for code in wanted if code not in store_prices],
        })
    return sorted(totals, key=lambda t: (len(t["missing"]), t["compare"]))


def best_split(prices: dict[tuple, dict[str, float]], wanted: dict[str, int], compare: dict | None = None) -> dict | None:
    """Cheapest way to buy everything in two stores: each product goes to the cheaper of the two (by compare)."""
    compare = compare or prices
    best = None
    for a, b in combinations(prices, 2):
        plan, total, comparable = {a: [], b: []}, 0.0, 0.0
        for code, qty in wanted.items():
            options = [(compare[s][code], s) for s in (a, b) if code in prices[s]]
            if not options:
                break  # neither store sells it
            _, store = min(options)
            plan[store].append(code)
            total += prices[store][code] * qty
            comparable += compare[store][code] * qty
        else:
            if plan[a] and plan[b] and (best is None or comparable < best["compare"]):
                best = {"total": round(total, 2), "compare": round(comparable, 2), "plan": plan}
    return best
