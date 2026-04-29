def main(
    apply_credits_result: dict,
    customers_result: dict,
    processing_results: list
):
    """
    Generate a summary report of the autopay processing run.
    """
    
    # Count results by status
    completed = []
    no_invoice = []
    payment_issue = []
    errors = []
    dry_run_success = []
    
    for r in processing_results:
        status = r.get("status")
        summary = {
            "name": r.get("customer_name"),
            "amount": r.get("amount_charged"),
            "invoices": r.get("invoices_paid", []),
            "notes": r.get("notes", [])
        }
        
        if status == "completed":
            completed.append(summary)
        elif status == "dry_run_success":
            dry_run_success.append(summary)
        elif status == "no_invoice":
            no_invoice.append(summary)
        elif status == "payment_issue":
            payment_issue.append(summary)
        elif status == "error":
            errors.append(summary)
    
    total_charged = sum(r.get("amount_charged", 0) or 0 for r in processing_results if r.get("status") in ["completed", "dry_run_success"])
    
    return {
        "billing_month": customers_result.get("billing_month"),
        "test_mode": customers_result.get("test_mode"),
        "dry_run": processing_results[0].get("dry_run") if processing_results else True,
        "summary": {
            "total_customers_processed": len(processing_results),
            "completed": len(completed),
            "dry_run_success": len(dry_run_success),
            "no_invoice": len(no_invoice),
            "payment_issue": len(payment_issue),
            "errors": len(errors),
            "total_amount_charged": total_charged
        },
        "credits_applied": {
            "total_found": apply_credits_result.get("total_maint_payments_found", 0),
            "applied": apply_credits_result.get("would_apply", 0),
            "skipped": apply_credits_result.get("skipped", 0)
        },
        "details": {
            "completed": completed,
            "dry_run_success": dry_run_success,
            "no_invoice": no_invoice,
            "payment_issue": payment_issue,
            "errors": errors
        }
    }
