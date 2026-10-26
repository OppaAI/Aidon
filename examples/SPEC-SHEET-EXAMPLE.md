# Spec Sheet

How to use this sheet: fill it in, save it, and attach it in the chat box.
Aidon reads it, verifies it, and skips the interview.
Template: specs/template.md in your Aidon home, and examples/SPEC-SHEET-TEMPLATE.md in the repo.
Docs: README, Spec sheet section.

This is a filled example, not a template. The customers, invoice numbers, and amounts below are fictional, for illustration only. Totals are calculated by code from the invoice amounts, not typed in by hand, and they reconcile as follows: Northside Bakery $755.50, Acme Consulting $2,020.00, Cedar Landscaping $0.00 paid in full, grand total due $2,775.50.

## Goal

Generate standard-format monthly statements for October 2026 from customer invoice accounts, calculate the total due for each customer with a full breakdown, and email each statement to the customer.

## Deliverables

- statement-2026-10-northside-bakery.pdf
- statement-2026-10-acme-consulting.pdf
- statement-2026-10-cedar-landscaping.pdf
- monthly-statements-2026-10-summary.csv
- Email sent to each customer with their statement attached

## Constraints

- Statement period is October 1 to October 31, 2026 only, invoices outside this period are excluded
- One statement per customer, in the standard format: customer info, opening balance, invoices, payments, credits, closing balance, and pay by date
- All amounts are in Canadian dollars, tax is already included in the invoice amounts, do not add tax again
- Totals are calculated by code from the invoice, payment, and credit amounts, never typed in by hand
- Pay by date is November 15, 2026 for every customer with a balance due
- A customer with a zero balance still receives a statement, marked paid in full, with no payment requested

## Budget

None

## Deadline

Statements emailed by November 5, 2026

## Recipients

- Northside Bakery, billing@northside.example
- Acme Consulting, billing@acme.example
- Cedar Landscaping, billing@cedar.example

## Files

- invoices-2026-10.csv
- customer-accounts-2026-10.csv
- statement-2026-10-northside-bakery.pdf
- statement-2026-10-acme-consulting.pdf
- statement-2026-10-cedar-landscaping.pdf
- monthly-statements-2026-10-summary.csv

## Notifications

- Email the owner a confirmation list after all statements are sent, with each customer, total due, and pay by date
- Report any failed email send immediately, do not mark that statement as sent

## Assumptions

- Invoice amounts in invoices-2026-10.csv are final and in Canadian dollars
- Customer billing emails in customer-accounts-2026-10.csv are current
- Opening balance for Northside Bakery is $0.00, for Acme Consulting is $120.00 from September, for Cedar Landscaping is $0.00
- Northside Bakery breakdown: opening $0.00, invoice INV-2026-1010 on October 5 for $640.00, invoice INV-2026-1014 on October 21 for $415.50, payment on October 28 of $300.00, closing balance $755.50
- Acme Consulting breakdown: opening $120.00, invoice INV-2026-1001 on October 3 for $1,200.00, invoice INV-2026-1002 on October 18 for $850.00, credit note CN-2026-002 on October 25 of $150.00, closing balance $2,020.00
- Cedar Landscaping breakdown: opening $0.00, invoice INV-2026-1022 on October 12 for $975.25, payment on October 30 of $975.25, closing balance $0.00 paid in full

## Open questions

None
