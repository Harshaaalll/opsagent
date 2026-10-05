# Northwind Traders: Accounts Payable procedures

## [AP-01] Entering a vendor invoice into the ERP
> also: vendor bill, payable, supplier invoice, book an invoice, record invoice, enter invoice
Procedure: (1) find the vendor's invoice in the vendor mail portal; (2) open the attached PDF and read it: the PDF is the
authoritative source for invoice number, invoice date, due date and total payable; (3) check the ERP for an existing bill with the
same vendor and invoice number BEFORE entering anything; (4) enter the bill through the ERP "new bill" form: vendor, invoice
number, invoice date, due date, currency, amount (the tax-inclusive total payable); (5) dates are entered as YYYY-MM-DD;
(6) after saving, confirm the bill appears in the ERP with the right values.

## [AP-02] Approval thresholds for ERP writes
> also: approval limit, who can approve, auto approve, sign off, spending authority
Saving a bill in the ERP is a financial write. The policy gate approves it automatically only when ALL hold: the vendor is on the
trusted list, the amount is at most INR 25,000, the currency is INR, the bill is not a duplicate, and the due date is not before
the invoice date. Anything else needs a human approval. Amounts above INR 100,000 always need a human, whatever else is true.

## [AP-03] Vendor identity
> also: lookalike vendor, similar name, which vendor, vendor name match
Match vendors on their exact legal name AND sender domain. "Acme Supplies Pvt Ltd" (billing@acme-supplies.test) and "Acme Supply Co"
(accounts@acmesupplyco.test) are two different companies. Never book one company's invoice against the other.

## [AP-04] Duplicates
> also: already entered, double entry, repeated invoice, same invoice twice
Never create a duplicate bill. If the ERP already holds a bill for that vendor and invoice number, do not enter it again: report
that it already exists, with the existing bill's details, and finish. Do not retry a save that may already have succeeded: check the ERP first.

## [AP-05] What counts as an invoice
> also: credit note, reminder, statement, promo, notification
Payment reminders, statements, promotional mail and credit notes are not invoices. The "latest invoice" means the most recent
message that carries an invoice PDF from that vendor, judged by invoice date, not by the latest email of any kind.

## [AP-06] Mismatch between email and PDF
> also: discrepancy, amount differs, conflicting amounts, different total
If the amount in the email body differs from the PDF total, do not guess. Ask a human which is correct before entering anything.

## [AP-07] Reporting back
> also: summary, evidence, confirmation, report, tell me when done
When finished, report what was entered (vendor, invoice number, dates, amount), where it came from, and the evidence that it was
verified in the ERP.
