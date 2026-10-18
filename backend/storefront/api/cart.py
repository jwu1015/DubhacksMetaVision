"""Cart: add to cart, no payment. One in-memory cart per server (no users yet).

    POST   /cart       {"product": Product}  -> Cart
    GET    /cart                             -> Cart
    DELETE /cart/{i}   (i = index in items)  -> Cart

app.py mounts this with app.include_router(cart.router).
"""
from fastapi import APIRouter, HTTPException

from storefront.models import AddToCartRequest, Cart, Product

router = APIRouter()


items: list[Product] = []


def dollars(amount: float) -> str:
    """Whole dollars for speech: 89.99 -> '90 dollars'."""
    n = round(amount)
    return f"{n} dollar" if n == 1 else f"{n} dollars"


def which(p: Product) -> str:
    return f"the {p.store} item" if p.store else "that item"


def cart_response(said: str = "") -> Cart:
    total = round(sum(p.price for p in items), 2)
    if items:
        status = f"Your cart has {len(items)} item{'' if len(items) == 1 else 's'}, {dollars(total)} total."
    else:
        status = "Your cart is empty."
    return Cart(items=items, total=total, spoken_summary=f"{said} {status}".strip())


@router.post("/cart", response_model=Cart)
async def add_to_cart(req: AddToCartRequest):
    items.append(req.product)
    return cart_response(f"Added {which(req.product)}, {dollars(req.product.price)}.")


@router.get("/cart", response_model=Cart)
async def get_cart():
    return cart_response()


@router.delete("/cart/{i}", response_model=Cart)
async def remove_from_cart(i: int):
    if not 0 <= i < len(items):
        raise HTTPException(404, f"no cart item {i}")
    return cart_response(f"Removed {which(items.pop(i))}.")
