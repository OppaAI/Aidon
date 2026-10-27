# Spec Sheet

How to use this sheet: fill it in, save it, and attach it in the chat box.
Aidon reads it, verifies it, and skips the interview.
Template: specs/template.md in your Aidon home, and examples/SPEC-SHEET-TEMPLATE.md in the repo.
Docs: README, Spec sheet section.

This is a filled example, not a template. The customers, invoice numbers, and amounts below are fictional, for illustration only. Totals are calculated by code from the invoice amounts, not typed in by hand, and they reconcile as follows: Northside Bakery $755.50, Acme Consulting $2,020.00, Cedar Landscaping $0.00 paid in full, grand total due $2,775.50.

SPECIAL REMINDER: Send only one customer's monthly statement PDF file to that customer's own email. Do not send other customers' statements to the wrong email. Never send a customer's statement to another customer, never CC or BCC other customers on a statement email, and never attach more than one customer's PDF to one email. Before sending, check that three things match: the customer name, the PDF filename, and the recipient email address. If any one of them does not match, stop, do not send, and report the mismatch to the owner.

SCHEDULING AND REVIEW: Accounting emails are drafts first, not sent immediately. Draft each statement email, schedule it to send at 3:00pm on the first Wednesday of the month, and let the AR department review the drafts before they send. For October 2026 statements, the send time is 3:00pm on Wednesday, November 4, 2026. AR review happens before that send time, AR can edit or cancel any draft before 3:00pm, and a draft AR rejects or flags is held and not sent at 3:00pm.

## Goal

Generate standard-format monthly statements for October 2026 from customer invoice accounts, calculate the total due for each customer with a full breakdown, draft each statement email, schedule it to send at 3:00pm on the first Wednesday of the month, and let the AR department review the drafts before they send.

## Deliverables

- statement-2026-10-northside-bakery.pdf
- statement-2026-10-acme-consulting.pdf
- statement-2026-10-cedar-landscaping.pdf
- monthly-statements-2026-10-summary.csv
- One draft email per customer, each with only that customer's statement PDF attached, to that customer's own email only, scheduled to send at 3:00pm on the first Wednesday of the month
- AR review list for the AR department, with each draft, customer, PDF filename, recipient email, total due, and scheduled send time

## Constraints

- SPECIAL REMINDER: Only send the monthly statement PDF file of one customer to its own email, do not send other customers to the wrong email, never CC or BCC another customer, never attach another customer's PDF, and check customer name, PDF filename, and recipient email match before every send, if they do not match, do not send
- Accounting emails are drafts first, never sent immediately, scheduled to send at 3:00pm on the first Wednesday of the month, AR department reviews the drafts before they send, AR can edit or cancel a draft before 3:00pm, a draft AR rejects or flags is held and not sent
- Statement period is October 1 to October 31, 2026 only, invoices outside this period are excluded
- One statement per customer, in the standard format: customer info, opening balance, invoices, payments, credits, closing balance, and pay by date
- All amounts are in Canadian dollars, tax is already included in the invoice amounts, do not add tax again
- Totals are calculated by code from the invoice, payment, and credit amounts, never typed in by hand
- Pay by date is November 15, 2026 for every customer with a balance due
- A customer with a zero balance still receives a statement, marked paid in full, with no payment requested

## Budget

None

## Deadline

Drafts ready for AR review by November 2, 2026, scheduled to send at 3:00pm on Wednesday, November 4, 2026, the first Wednesday of November 2026

## Recipients

- Northside Bakery, billing@northside.example
- Acme Consulting, billing@acme.example
- Cedar Landscaping, billing@cedar.example
- AR department, for review of all statement drafts before they send

## Files

- invoices-2026-10.csv
- customer-accounts-2026-10.csv
- statement-2026-10-northside-bakery.pdf
- statement-2026-10-acme-consulting.pdf
- statement-2026-10-cedar-landscaping.pdf
- monthly-statements-2026-10-summary.csv

## Notifications

- Notify the AR department when all statement drafts are ready for review, with each draft, customer, PDF filename, recipient email, total due, and scheduled send time of 3:00pm on the first Wednesday of the month
- Email the owner a confirmation list after all statements are sent, with each customer, PDF filename, recipient email, total due, and pay by date, so a wrong-email send can be spotted
- Report any failed email send immediately, do not mark that statement as sent
- Report any customer name, PDF filename, and recipient email mismatch immediately, do not send that statement until the owner confirms the correct email

## Assumptions

- Invoice amounts in invoices-2026-10.csv are final and in Canadian dollars
- Customer billing emails in customer-accounts-2026-10.csv are current
- Each customer in customer-accounts-2026-10.csv maps to exactly one billing email, no two customers share an email address, if they do, stop and ask the owner before sending anything
- Opening balance for Northside Bakery is $0.00, for Acme Consulting is $120.00 from September, for Cedar Landscaping is $0.00
- Northside Bakery breakdown: opening $0.00, invoice INV-2026-1010 on October 5 for $640.00, invoice INV-2026-1014 on October 21 for $415.50, payment on October 28 of $300.00, closing balance $755.50
- Acme Consulting breakdown: opening $120.00, invoice INV-2026-1001 on October 3 for $1,200.00, invoice INV-2026-1002 on October 18 for $850.00, credit note CN-2026-002 on October 25 of $150.00, closing balance $2,020.00
- Cedar Landscaping breakdown: opening $0.00, invoice INV-2026-1022 on October 12 for $975.25, payment on October 30 of $975.25, closing balance $0.00 paid in full

## Open questions

None
