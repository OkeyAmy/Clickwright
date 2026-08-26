"""The mock legacy portal — the thing that has no API.

Deliberately awful in the ways real 2000s enterprise portals are awful:
server-rendered, table layout, session cookies, a multi-step wizard that
rejects out-of-order access, ASP.NET-style generated element ids, and
validation that only tells you about one problem at a time.

Two tenants (vendor, benefits) so the fleet story has plurality.
Set DRIFT=1 to mutate the UI — this is what the healer has to survive.

    uv run uvicorn portal.app:app --port 8081
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

DRIFT = os.getenv("DRIFT", "0") == "1"
CREDENTIALS = {"demo-user": "demo-pass", "benefits-user": "demo-pass"}

TENANTS: dict[str, dict[str, Any]] = {
    "vendor": {
        "name": "VendorNet 4.2",
        "operation": "Expense Claims",
        "record": "claim",
        "fields": [
            {"id": "ctl00_claimType", "name": "claimType", "label": "Claim type",
             "widget": "select", "options": ["— select —", "Travel", "Equipment", "Subsistence"]},
            {"id": "ctl00_ref", "name": "ref", "label": "Invoice ref", "widget": "text"},
            {"id": "ctl00_amt", "name": "amt", "label": "Amount (USD)", "widget": "text"},
            {"id": "ctl00_costCentre", "name": "costCentre", "label": "Cost centre", "widget": "text"},
        ],
    },
    "benefits": {
        "name": "BenefitsDesk",
        "operation": "Dependent Enrolment",
        "record": "enrolment",
        "fields": [
            {"id": "ctl00_relation", "name": "relation", "label": "Relationship",
             "widget": "select", "options": ["— select —", "Spouse", "Child", "Partner"]},
            {"id": "ctl00_fullName", "name": "fullName", "label": "Full name", "widget": "text"},
            {"id": "ctl00_dob", "name": "dob", "label": "Date of birth", "widget": "text"},
            {"id": "ctl00_plan", "name": "plan", "label": "Plan code", "widget": "text"},
        ],
    },
}

app = FastAPI(title="Legacy portal (mock)")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("PORTAL_SECRET", secrets.token_hex(16)))
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _ctx(request: Request, tenant: str, **extra: Any) -> dict[str, Any]:
    cfg = TENANTS[tenant]
    return {
        "request": request,
        "tenant": tenant,
        "cfg": cfg,
        "drift": DRIFT,
        # under drift the submit control becomes a <button> with a testid, and the
        # confirmation copy changes — both break a naively compiled playbook
        "submit_label": "Send Claim" if DRIFT else f"Submit {cfg['record'].title()}",
        **extra,
    }


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse("/vendor/login", status_code=302)


@app.get("/{tenant}/login", response_class=HTMLResponse)
def login_form(request: Request, tenant: str, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", _ctx(request, tenant, error=error))


@app.post("/{tenant}/login")
def login(request: Request, tenant: str, username: str = Form(""), password: str = Form("")):
    if CREDENTIALS.get(username) != password:
        return RedirectResponse(f"/{tenant}/login?error=Invalid+credentials", status_code=302)
    request.session["user"] = username
    request.session["stage"] = 1
    return RedirectResponse(f"/{tenant}/new", status_code=302)


@app.get("/{tenant}/new", response_class=HTMLResponse)
def new_record(request: Request, tenant: str, error: str | None = None):
    if not request.session.get("user"):
        return RedirectResponse(f"/{tenant}/login", status_code=302)
    request.session["stage"] = 2
    return templates.TemplateResponse(request, "form.html", _ctx(request, tenant, error=error))


@app.post("/{tenant}/new")
async def submit_record(request: Request, tenant: str):
    if not request.session.get("user"):
        return RedirectResponse(f"/{tenant}/login", status_code=302)
    if request.session.get("stage") != 2:
        # the wizard refuses out-of-order access, as they all do
        return RedirectResponse(f"/{tenant}/new?error=Session+step+out+of+order", status_code=302)

    form = await request.form()
    cfg = TENANTS[tenant]

    # one error at a time, in field order — the classic
    for field in cfg["fields"]:
        value = (form.get(field["name"]) or "").strip()
        if not value or value.startswith("—"):
            return RedirectResponse(
                f"/{tenant}/new?error={field['label']}+is+required+by+policy+14-B", status_code=302
            )

    request.session["stage"] = 3
    request.session["record"] = {f["name"]: form.get(f["name"]) for f in cfg["fields"]}
    return RedirectResponse(f"/{tenant}/done", status_code=302)


@app.get("/{tenant}/done", response_class=HTMLResponse)
def done(request: Request, tenant: str):
    if request.session.get("stage") != 3:
        return RedirectResponse(f"/{tenant}/new", status_code=302)
    cfg = TENANTS[tenant]
    reference = f"{cfg['record'][:3].upper()}-{secrets.token_hex(3).upper()}"
    # drift also changes the confirmation copy, breaking the compiled assertion
    message = (
        f"{cfg['record'].title()} submitted for approval"
        if DRIFT
        else f"{cfg['record'].title()} received"
    )
    request.session["stage"] = 1
    return templates.TemplateResponse(
        request, "done.html", _ctx(request, tenant, reference=reference, message=message)
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "drift": DRIFT}
