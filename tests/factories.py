"""Builds the connector the tests drive.

These are the same shapes the distiller emits; the tests assert the runtime can
execute a playbook against the real portal, independently of any model call.
"""

from app.connectors.models import (
    Action,
    Connector,
    ConnectorVersion,
    InputField,
    PlaybookStep,
    Selector,
)


def sel(primary: str, *fallbacks: str) -> Selector:
    return Selector(primary=primary, fallbacks=list(fallbacks))


def vendor_connector(base_url: str) -> Connector:
    return Connector(
        id="vendor-portal",
        portal="VendorNet 4.2",
        operation="expense-claim",
        base_url=base_url,
        owner="sa-connector-vendor@clickwright.iam",
    )


def vendor_version(base_url: str, version: str = "1.0.0") -> ConnectorVersion:
    steps = [
        PlaybookStep(index=1, action=Action.NAVIGATE, url=f"{base_url}/vendor/login"),
        PlaybookStep(index=2, action=Action.TYPE, selector=sel("#ctl00_user"), value_from="username"),
        PlaybookStep(index=3, action=Action.TYPE, selector=sel("#ctl00_pass"), value_from="password"),
        PlaybookStep(index=4, action=Action.CLICK, selector=sel("#ctl00_signin")),
        PlaybookStep(index=5, action=Action.SELECT, selector=sel("#ctl00_claimType"), value_from="claim_type"),
        PlaybookStep(index=6, action=Action.TYPE, selector=sel("#ctl00_ref"), value_from="invoice_ref"),
        PlaybookStep(index=7, action=Action.TYPE, selector=sel("#ctl00_amt"), value_from="amount"),
        PlaybookStep(index=8, action=Action.TYPE, selector=sel("#ctl00_costCentre"), value_from="cost_centre"),
        PlaybookStep(
            index=9,
            action=Action.CLICK,
            # only the id — the drifted portal replaces this control entirely,
            # which is exactly the failure the healer exists to catch
            selector=sel("#ctl00_submit"),
            expect_text="Claim received",
        ),
    ]
    inputs = [
        InputField(name="claim_type", description="Travel, Equipment or Subsistence", example="Travel"),
        InputField(name="invoice_ref", description="Supplier invoice reference", example="INV-1"),
        InputField(name="amount", type="number", description="Claim total in USD", example="10.00"),
        InputField(name="cost_centre", description="Cost centre code", example="CC-4410"),
    ]
    return ConnectorVersion(version=version, steps=steps, inputs=inputs, source_run_id="run_test")


CLAIM = {
    "claim_type": "Travel",
    "invoice_ref": "INV-2026-Q3-4471",
    "amount": "284.50",
    "cost_centre": "CC-4410",
}
