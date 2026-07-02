//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import { main as updateTask } from "/f/ION/api/update_task"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: DRY-RUN the ION task-edit endpoint on WAITES (5954394 / cust 1128297): set InvoiceType
// to 9 = "Per Visit Itemized (list consumables)". dry_run=true returns the payload without writing.
export async function main() {
  const r: any = await updateTask("5954394", "1128297", { InvoiceType: "9" }, true)
  return { dry_run: r.dry_run, would_post_to: r.would_post_to, changed: r.changed, field_count: r.field_count,
    invoice_type_in_payload: r.payload_preview?.InvoiceType, itemcost: r.payload_preview?.itemcost, stopPayFixed: r.payload_preview?.StopPayFixed }
}
