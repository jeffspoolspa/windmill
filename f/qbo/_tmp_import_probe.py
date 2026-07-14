import f.billing._lib.qbo as q

def main():
    names = [n for n in dir(q) if not n.startswith("__")]
    return {"has_set_rate_limiter": hasattr(q, "set_rate_limiter"),
            "has_claim": hasattr(q, "_claim"),
            "has_update_invoice_sparse": hasattr(q, "update_invoice_sparse"),
            "n_names": len(names), "sample": sorted(names)[:12]}
