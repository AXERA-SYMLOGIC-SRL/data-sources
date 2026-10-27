"""The canonical ERP entities: what every ERP adapter fills in, the same way.

ERPs share no API, but they share a business: parties, products, orders and their lines,
invoices, payments, stock, production, CRM, projects, people. Each entity here names its core
columns, in one vocabulary for every ERP, so that what is built on top (tables, views, an agent's
queries) doesn't change with the ERP. An adapter maps each entity it supports onto its own
records and keeps the complete native record beside the core columns (`raw`), so nothing the ERP
holds is lost when a core column doesn't cover it.

Conventions:

- Every entity has `id` (the ERP's own key, as text) and `updated_at` (when the ERP last changed
  the record, UTC): they are not listed in `columns`.
- A `ref` column holds the `id` of a record of another entity (`references`). The adapter also
  fills `<name without _id>_name` with that record's display name when the ERP returns one
  (`partner_id` → `partner_name`), so most questions need no join.
- Amounts are in the document's currency (`currency`, a code), except where a column says
  `_company` (the company's currency). Quantities are in the product's unit.
- `state` and the other enumerations keep the ERP's own values; `status` columns, where present,
  are normalized (e.g. `crm_leads.status`: open / won / lost).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ColumnType = Literal["text", "number", "integer", "boolean", "date", "datetime", "ref"]


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    description: str = ""
    #: For a `ref` column: the entity it points to.
    references: str | None = None

    @property
    def name_column(self) -> str | None:
        """The companion display-name column of a `ref` (`partner_id` → `partner_name`)."""
        if self.type != "ref":
            return None
        return f"{self.name.removesuffix('_id')}_name"


@dataclass(frozen=True)
class Entity:
    name: str
    description: str
    columns: tuple[Column, ...] = field(default_factory=tuple)

    def column(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)

    @property
    def references(self) -> dict[str, str]:
        """`{column: entity}` for every `ref` column."""
        return {c.name: c.references for c in self.columns if c.references}


def ref(name: str, entity: str, description: str = "") -> Column:
    return Column(name, "ref", description, entity)


def text(name: str, description: str = "") -> Column:
    return Column(name, "text", description)


def number(name: str, description: str = "") -> Column:
    return Column(name, "number", description)


def date(name: str, description: str = "") -> Column:
    return Column(name, "date", description)


def datetime(name: str, description: str = "") -> Column:
    return Column(name, "datetime", description)


def boolean(name: str, description: str = "") -> Column:
    return Column(name, "boolean", description)


COMPANY = ref("company_id", "companies", "The company (legal entity) the record belongs to.")

CATALOG: tuple[Entity, ...] = (
    Entity(
        "companies",
        "The legal entities in the ERP. Every document belongs to one.",
        (
            text("name"),
            text("currency", "Accounting currency code."),
            text("country"),
            ref("parent_id", "companies"),
        ),
    ),
    Entity(
        "currencies",
        "Currencies known to the ERP, with the latest rate against the company currency.",
        (text("name", "ISO code."), text("symbol"), boolean("active"), number("rate")),
    ),
    Entity(
        "partners",
        "Customers, vendors and their contacts (one entity: a party can be both).",
        (
            text("name"),
            boolean("is_company"),
            ref("parent_id", "partners", "The company a contact works for."),
            boolean("is_customer"),
            boolean("is_supplier"),
            text("email"),
            text("phone"),
            text("city"),
            text("country"),
            text("vat", "Tax id."),
            ref("salesperson_id", "users"),
            boolean("active"),
            datetime("created_at"),
            COMPANY,
        ),
    ),
    Entity(
        "users",
        "People who sign in to the ERP (salespeople, buyers, accountants).",
        (text("name"), text("login"), boolean("active"), COMPANY),
    ),
    Entity(
        "product_categories",
        "Product categories, a tree.",
        (text("name"), text("full_name"), ref("parent_id", "product_categories")),
    ),
    Entity(
        "products",
        "Products and services (one row per sellable variant).",
        (
            text("name"),
            text("sku", "Internal reference."),
            text("barcode"),
            text("type", "goods, consumable, service or combo."),
            boolean("is_stocked", "Inventory is tracked for it."),
            ref("category_id", "product_categories"),
            number("list_price", "Sales price, company currency."),
            number("cost", "Unit cost, company currency."),
            text("uom", "Unit of measure."),
            boolean("can_be_sold"),
            boolean("can_be_purchased"),
            boolean("active"),
            COMPANY,
        ),
    ),
    Entity(
        "warehouses",
        "Warehouses.",
        (text("name"), text("code"), COMPANY),
    ),
    Entity(
        "locations",
        "Stock locations. Only `internal` ones hold the company's own stock; the others are where "
        "stock comes from or goes to (supplier, customer, production, inventory loss, transit).",
        (
            text("name"),
            text("full_name"),
            text("usage", "internal, supplier, customer, production, inventory, transit or view."),
            ref("warehouse_id", "warehouses"),
            boolean("active"),
            COMPANY,
        ),
    ),
    Entity(
        "sale_orders",
        "Sales quotations and orders.",
        (
            text("number"),
            ref("partner_id", "partners", "The customer."),
            datetime("date", "Order date (quotation date until confirmed)."),
            text("state", "draft, sent, sale (confirmed) or cancel."),
            text("invoice_status", "nothing, to invoice, invoiced, upselling."),
            text("delivery_status", "pending, started, partial, full."),
            ref("salesperson_id", "users"),
            text("team", "Sales team."),
            text("currency"),
            number("amount_untaxed"),
            number("amount_tax"),
            number("amount_total"),
            number("margin", "Sale price minus cost, untaxed."),
            text("origin", "Source document."),
            datetime("created_at"),
            COMPANY,
        ),
    ),
    Entity(
        "sale_order_lines",
        "Lines of sales orders.",
        (
            ref("order_id", "sale_orders"),
            ref("product_id", "products"),
            text("description"),
            number("quantity", "Ordered quantity."),
            number("quantity_delivered"),
            number("quantity_invoiced"),
            number("unit_price"),
            number("discount", "Percent."),
            number("amount_untaxed"),
            number("amount_total"),
            number("unit_cost", "Product cost when sold."),
            number("margin"),
            text("currency"),
            text("state", "The order's state."),
            COMPANY,
        ),
    ),
    Entity(
        "purchase_orders",
        "Requests for quotation and purchase orders to vendors.",
        (
            text("number"),
            ref("partner_id", "partners", "The vendor."),
            datetime("date", "Order date."),
            datetime("date_approved", "When it was confirmed."),
            datetime("date_planned", "Expected arrival."),
            text(
                "state", "draft, sent, to approve, purchase (confirmed), done (locked) or cancel."
            ),
            text("receipt_status", "pending, partial, full."),
            text("invoice_status", "no, to invoice, invoiced."),
            ref("buyer_id", "users"),
            text("currency"),
            number("amount_untaxed"),
            number("amount_tax"),
            number("amount_total"),
            text("origin"),
            COMPANY,
        ),
    ),
    Entity(
        "purchase_order_lines",
        "Lines of purchase orders.",
        (
            ref("order_id", "purchase_orders"),
            ref("product_id", "products"),
            text("description"),
            number("quantity"),
            number("quantity_received"),
            number("quantity_billed"),
            number("unit_price"),
            number("amount_untaxed"),
            number("amount_total"),
            datetime("date_planned"),
            text("currency"),
            text("state"),
            COMPANY,
        ),
    ),
    Entity(
        "invoices",
        "Customer invoices and credit notes (direction `out`), vendor bills and refunds (`in`).",
        (
            text("number"),
            text("direction", "out (sales: receivable) or in (purchases: payable)."),
            text("kind", "invoice or refund. A refund reduces revenue (out) or cost (in)."),
            ref("partner_id", "partners"),
            date("invoice_date"),
            date("due_date"),
            date("accounting_date"),
            text("state", "draft, posted or cancel. Only posted invoices count."),
            text("payment_state", "not_paid, in_payment, paid, partial, reversed."),
            text("currency"),
            number("amount_untaxed"),
            number("amount_tax"),
            number("amount_total"),
            number(
                "amount_due",
                "What is still owed, in `currency`; on a credit note, what is still to "
                "be refunded (also positive).",
            ),
            number(
                "amount_total_company", "Total in the company currency, signed: refunds negative."
            ),
            text("origin", "Source document (e.g. the sales order number)."),
            ref("salesperson_id", "users"),
            COMPANY,
        ),
    ),
    Entity(
        "invoice_lines",
        "Product lines of invoices and bills (not taxes or payment terms).",
        (
            ref("invoice_id", "invoices"),
            ref("product_id", "products"),
            text("description"),
            number("quantity"),
            number("unit_price"),
            number("discount", "Percent."),
            number("amount_untaxed"),
            number("amount_total"),
            ref("account_id", "accounts"),
            text("currency"),
            COMPANY,
        ),
    ),
    Entity(
        "accounts",
        "The chart of accounts.",
        (
            text("code"),
            text("name"),
            text("type", "The ERP's account type (receivable, income, expense, ...)."),
            boolean("reconcile"),
        ),
    ),
    Entity(
        "journal_items",
        "Every accounting line (the general ledger), from invoices, payments, stock valuation and "
        "manual entries.",
        (
            text("entry", "The journal entry's number."),
            date("date"),
            ref("account_id", "accounts"),
            ref("partner_id", "partners"),
            text("label"),
            text("journal"),
            number("debit", "Company currency."),
            number("credit", "Company currency."),
            number("balance", "Debit minus credit, company currency."),
            text("state", "The entry's state: posted, draft or cancel. Only posted lines count."),
            boolean("reconciled"),
            COMPANY,
        ),
    ),
    Entity(
        "payments",
        "Customer and vendor payments.",
        (
            text("number"),
            ref("partner_id", "partners"),
            date("date"),
            text("direction", "inbound (received) or outbound (paid)."),
            text("partner_type", "customer or supplier."),
            number("amount"),
            text("currency"),
            text("state"),
            text("journal", "Bank or cash journal."),
            text("memo"),
            COMPANY,
        ),
    ),
    Entity(
        "stock_pickings",
        "Transfers: receipts from vendors, deliveries to customers, internal moves.",
        (
            text("number"),
            text("type", "incoming, outgoing or internal (also mrp_operation, dropship)."),
            ref("partner_id", "partners"),
            text("origin", "Source document (order number), free text."),
            ref("sale_order_id", "sale_orders", "The sales order a delivery ships."),
            ref("purchase_order_id", "purchase_orders", "The purchase order a receipt brings in."),
            datetime("scheduled_date"),
            datetime("done_date"),
            text("state", "draft, waiting, confirmed, assigned (ready), done or cancel."),
            ref("location_id", "locations", "From."),
            ref("location_dest_id", "locations", "To."),
            COMPANY,
        ),
    ),
    Entity(
        "stock_moves",
        "Product movements between locations; `done` moves make up stock history.",
        (
            ref("picking_id", "stock_pickings"),
            ref("product_id", "products"),
            datetime("date", "When it was done (planned date until then)."),
            number("quantity", "Quantity moved, in the product's unit."),
            text("uom"),
            ref("location_id", "locations", "From."),
            ref("location_dest_id", "locations", "To."),
            text("state", "draft, waiting, confirmed, assigned, done or cancel."),
            text("reference"),
            text("origin"),
            number("unit_cost", "Unit value when the move was valued, company currency."),
            ref("sale_line_id", "sale_order_lines"),
            ref("purchase_line_id", "purchase_order_lines"),
            ref("production_id", "manufacturing_orders", "The order it produced (finished goods)."),
            ref(
                "raw_material_production_id",
                "manufacturing_orders",
                "The order it was consumed by (components).",
            ),
            COMPANY,
        ),
    ),
    Entity(
        "stock_quants",
        "Stock on hand now, per product and location (internal locations are the company's stock).",
        (
            ref("product_id", "products"),
            ref("location_id", "locations"),
            number("quantity", "On hand."),
            number("reserved_quantity"),
            text("lot"),
            number("value", "Stock value, company currency."),
            COMPANY,
        ),
    ),
    Entity(
        "manufacturing_orders",
        "Production orders: components consumed into finished products.",
        (
            text("number"),
            ref("product_id", "products"),
            number("quantity", "Planned quantity."),
            number("quantity_produced"),
            text("state", "draft, confirmed, progress, to_close, done or cancel."),
            datetime("date_start"),
            datetime("date_finished"),
            text("origin"),
            ref("responsible_id", "users"),
            COMPANY,
        ),
    ),
    Entity(
        "crm_leads",
        "Leads and opportunities in the sales pipeline.",
        (
            text("name"),
            text("type", "lead or opportunity."),
            ref("partner_id", "partners"),
            text("stage"),
            text("status", "open, won or lost."),
            number("expected_revenue", "Expected revenue, in `currency`."),
            text("currency"),
            number("probability", "Percent."),
            ref("salesperson_id", "users"),
            text("team"),
            text("lost_reason"),
            date("deadline", "Expected closing."),
            datetime("created_at"),
            datetime("closed_at"),
            COMPANY,
        ),
    ),
    Entity(
        "employees",
        "Employees.",
        (
            text("name"),
            text("job_title"),
            text("department"),
            ref("manager_id", "employees"),
            ref("user_id", "users"),
            boolean("active"),
            COMPANY,
        ),
    ),
    Entity(
        "projects",
        "Projects (internal, or delivered to a customer).",
        (
            text("name"),
            ref("partner_id", "partners", "The customer, if any."),
            ref("manager_id", "users"),
            boolean("active"),
            COMPANY,
        ),
    ),
    Entity(
        "tasks",
        "Project tasks.",
        (
            text("name"),
            ref("project_id", "projects"),
            text("stage"),
            text("state", "The ERP's task state."),
            number("allocated_hours"),
            number("effective_hours", "Hours logged."),
            date("deadline"),
            ref("partner_id", "partners"),
            datetime("created_at"),
            COMPANY,
        ),
    ),
    Entity(
        "timesheets",
        "Hours logged by employees on projects and tasks.",
        (
            date("date"),
            ref("employee_id", "employees"),
            ref("project_id", "projects"),
            ref("task_id", "tasks"),
            text("description"),
            number("hours"),
            number("cost", "Cost of the hours, company currency (negative: an expense)."),
            COMPANY,
        ),
    ),
    Entity(
        "expenses",
        "Expenses employees report for reimbursement.",
        (
            text("description"),
            ref("employee_id", "employees"),
            ref("product_id", "products", "The expense category."),
            date("date"),
            number("amount"),
            text("currency"),
            text("state"),
            COMPANY,
        ),
    ),
    Entity(
        "pos_orders",
        "Point-of-sale (shop and restaurant) orders.",
        (
            text("number"),
            text("shop", "The point of sale."),
            datetime("date"),
            ref("partner_id", "partners"),
            text("state", "draft, paid, done (posted), invoiced or cancel."),
            number("amount_total"),
            number("amount_tax"),
            number("amount_paid"),
            text("currency"),
            ref("salesperson_id", "users"),
            COMPANY,
        ),
    ),
    Entity(
        "pos_order_lines",
        "Lines of point-of-sale orders.",
        (
            ref("order_id", "pos_orders"),
            ref("product_id", "products"),
            number("quantity"),
            number("unit_price"),
            number("discount", "Percent."),
            number("amount_untaxed"),
            number("amount_total"),
            COMPANY,
        ),
    ),
)

#: What a column means when its entity doesn't say more: the conventions every adapter follows.
COMMON: dict[str, str] = {
    "name": "Name as shown in the ERP.",
    "number": "Document number as shown in the ERP (e.g. S00042, INV/2026/0001).",
    "state": "The ERP's own status value.",
    "description": "The line's text as printed on the document.",
    "currency": "ISO code of the currency the amounts are in.",
    "amount_untaxed": "Amount without tax, in `currency`. Never negative: credit notes and refunds "
    "are positive too (tell them apart by `kind`).",
    "amount_tax": "Tax, in `currency`. Never negative, also on credit notes.",
    "amount_total": "Total with tax, in `currency`. Never negative: credit notes and refunds are "
    "positive too.",
    "quantity": "Quantity, in the product's unit of measure.",
    "unit_price": "Price per unit before discount, in `currency`.",
    "margin": "Untaxed revenue minus cost, in `currency` (overstated where the product has no "
    "cost).",
    "date": "The document's date (UTC).",
    "created_at": "When the record was created in the ERP (UTC).",
    "active": "False when the record is archived in the ERP (archived records are kept).",
    "partner_id": "The customer or vendor (`partners`).",
    "product_id": "The product (`products`).",
    "order_id": "The order the line belongs to.",
    "origin": "Reference of the document it came from, free text (often an order number).",
    "email": "Email address.",
    "phone": "Phone number.",
    "city": "City.",
    "country": "Country name.",
    "uom": "Unit of measure.",
    "journal": "The accounting journal (sales, purchases, a bank, ...).",
    "salesperson_id": "The salesperson (`users`).",
    "deadline": "Due or expected date.",
}


def _default_description(column: Column) -> str:
    """A readable description from the column's name and type, when neither the entity nor `COMMON`
    has one."""
    if column.name in COMMON:
        return COMMON[column.name]
    words = column.name.removesuffix("_id").replace("_", " ")
    if column.type == "ref":
        return f"The {words} (`{column.references}`)."
    if column.type == "datetime":
        return f"{words.capitalize()} (UTC)."
    if column.type == "date":
        return f"{words.capitalize()} (a date)."
    if column.type == "boolean":
        return f"Whether: {words}."
    if column.name.startswith("quantity_"):
        return f"Quantity {words.removeprefix('quantity ')}, in the product's unit of measure."
    return f"{words.capitalize()}."


def _described(entity: Entity) -> Entity:
    columns = tuple(
        c if c.description else Column(c.name, c.type, _default_description(c), c.references)
        for c in entity.columns
    )
    return Entity(entity.name, entity.description, columns)


CATALOG = tuple(_described(e) for e in CATALOG)
ENTITIES: dict[str, Entity] = {e.name: e for e in CATALOG}


def entity(name: str) -> Entity:
    try:
        return ENTITIES[name]
    except KeyError:
        raise KeyError(f"Unknown ERP entity {name!r}") from None
