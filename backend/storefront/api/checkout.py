"""Checkout, a stand-in until payments land: it "pays" at once and shows the success page."""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from storefront import config
from storefront.models import CheckoutRequest, CheckoutResponse

router = APIRouter()


@router.post("/checkout", response_model=CheckoutResponse)
async def checkout_route(req: CheckoutRequest):
    return CheckoutResponse(checkout_url=f"{config.PUBLIC_BASE_URL}/checkout/success")


@router.get("/checkout/success", response_class=HTMLResponse)
async def checkout_success_route():
    return ("<meta name=viewport content='width=device-width'>"
            "<h1 style='font-family:sans-serif;text-align:center;margin-top:40vh'>Order placed!</h1>")
