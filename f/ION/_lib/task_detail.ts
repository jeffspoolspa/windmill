//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION task edit form: deepest per-task READ + the WRITE-BACK path (ADR 002).
// (playwright pinned because this imports f/ION/_lib/session, which uses it for login.)
//
// GET /tasks/addTask.cfm?EventID=<ion_task_id>&isIFrame=1 returns the task edit
// form (prime the customer first via customerTabs?customerid=X -- same session
// pattern as taskList). The <form action="/tasks/addTask.cfm?EventID=<id>"
// method="post"> POSTS BACK TO ITSELF, so submitting it edits the task in ION.
//
// Richer than taskList: day1..day7 = Sun..Sat, each a tech <select> (value = ION
// employee id; empty = not serviced) -> EXPLICIT PER-DAY TECH. Plus ServiceType,
// profile, ServiceRepeat, InvoiceType, InvoiceDate (enums), StartsOn/EndsOn,
// StopPayFixed, itemcost, tasknote, flags, and Old* hidden fields (prior values
// for change detection on save).
//
// WRITE-BACK is dry_run-first (ADR 002): updateTask(dry_run=true) builds + returns
// the exact POST payload it WOULD send WITHOUT submitting. A live write only fires
// with dry_run=false. Single write path; idempotent via Old* diff; the next task/
// schedule sync is the [reflection] that pulls the change back into our cache.

import "playwright@1.40.0"
import { ionFetch, ionFetchText, type IonSession } from "/f/ION/_lib/session"
import { parse } from "node-html-parser@6.1.13"

const DOW_FIELDS = ["day1", "day2", "day3", "day4", "day5", "day6", "day7"] // index 0=Sun .. 6=Sat
const DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
const norm = (s: string) => (s || "").replace(/\s+/g, " ").trim()

export interface TaskDetail {
  ionTaskId: string
  customerId: string
  serviceType: { value: string; text: string }
  profile: { value: string; text: string }
  serviceRepeat: { value: string; text: string }
  invoiceType: { value: string; text: string }
  invoiceDate: { value: string; text: string }
  startsOn: string
  endsOn: string
  stopPayFixed: string
  itemCost: string
  taskNote: string
  perDayTech: { dow: number; dayName: string; techId: string; techName: string }[]
  flags: Record<string, string>
}

function pickTaskForm(root: any): any {
  const forms = root.querySelectorAll("form")
  for (const f of forms) if ((f.getAttribute("action") || "").includes("addTask")) return f
  return forms[0] ?? null
}

/** Serialize the form's current state into the name->value map a browser would POST. */
function serializeForm(form: any): Record<string, string> {
  const out: Record<string, string> = {}
  for (const inp of form.querySelectorAll("input")) {
    const name = inp.getAttribute("name")
    if (!name) continue
    const type = (inp.getAttribute("type") || "text").toLowerCase()
    if (type === "radio" || type === "checkbox") {
      if (inp.getAttribute("checked") != null) out[name] = inp.getAttribute("value") ?? "on"
    } else {
      out[name] = inp.getAttribute("value") ?? ""
    }
  }
  for (const sel of form.querySelectorAll("select")) {
    const name = sel.getAttribute("name")
    if (!name) continue
    const opt = sel.querySelector("option[selected]")
    out[name] = opt ? (opt.getAttribute("value") ?? "") : ""
  }
  for (const ta of form.querySelectorAll("textarea")) {
    const name = ta.getAttribute("name")
    if (!name) continue
    out[name] = ta.text ?? ""
  }
  return out
}

function selText(form: any, name: string): { value: string; text: string } {
  const sel = form.querySelector(`select[name="${name}"]`)
  if (!sel) return { value: "", text: "" }
  const opt = sel.querySelector("option[selected]")
  return { value: opt?.getAttribute("value") ?? "", text: norm(opt?.text ?? "") }
}

export async function fetchTaskFormHtml(session: IonSession, ionTaskId: string | number, ionCustId: string | number = ""): Promise<string> {
  if (ionCustId) await ionFetchText(session, `${session.ionOrigin}/customers/customerTabs.cfm?customerid=${ionCustId}`)
  return ionFetchText(session, `${session.ionOrigin}/tasks/addTask.cfm?EventID=${ionTaskId}&isIFrame=1`)
}

export function parseTaskForm(html: string): { fields: Record<string, string>; detail: TaskDetail; dayRoster: Record<string, string> } {
  const form = pickTaskForm(parse(html))
  if (!form) throw new Error("addTask form not found")
  const fields = serializeForm(form)

  const perDayTech: TaskDetail["perDayTech"] = []
  const dayRoster: Record<string, string> = {}
  DOW_FIELDS.forEach((dn, i) => {
    const sel = form.querySelector(`select[name="${dn}"]`)
    if (!sel) return
    if (Object.keys(dayRoster).length === 0) {
      for (const o of sel.querySelectorAll("option")) {
        const v = o.getAttribute("value") || ""
        if (v) dayRoster[v] = norm(o.text)
      }
    }
    const opt = sel.querySelector("option[selected]")
    const v = opt?.getAttribute("value") ?? ""
    if (v) perDayTech.push({ dow: i, dayName: DOW_NAMES[i], techId: v, techName: norm(opt?.text ?? "") })
  })

  const detail: TaskDetail = {
    ionTaskId: fields["EventID"] ?? "",
    customerId: fields["CustomerID"] ?? "",
    serviceType: selText(form, "ServiceType"),
    profile: selText(form, "profileid"),
    serviceRepeat: selText(form, "ServiceRepeat"),
    invoiceType: selText(form, "InvoiceType"),
    invoiceDate: selText(form, "InvoiceDate"),
    startsOn: fields["StartsOn"] ?? "",
    endsOn: fields["EndsOn"] ?? "",
    stopPayFixed: fields["StopPayFixed"] ?? "",
    itemCost: fields["itemcost"] ?? "",
    taskNote: fields["tasknote"] ?? "",
    perDayTech,
    flags: {
      sendlog: fields["sendlog"] ?? "", SendConsumables: fields["SendConsumables"] ?? "",
      sendtechnote: fields["sendtechnote"] ?? "", SendFiles: fields["SendFiles"] ?? "",
      imgRequired: fields["imgRequired"] ?? "",
    },
  }
  return { fields, detail, dayRoster }
}

export async function getTaskDetail(session: IonSession, ionTaskId: string | number, ionCustId: string | number = "") {
  return parseTaskForm(await fetchTaskFormHtml(session, ionTaskId, ionCustId))
}

/**
 * Write-back. dry_run=true (default) re-reads the form, applies `changes`, and
 * returns the exact payload it WOULD POST -- without submitting. dry_run=false
 * actually POSTs (single write path; the next sync reflects the change back).
 */
export async function updateTask(
  session: IonSession,
  ionTaskId: string | number,
  ionCustId: string | number,
  changes: Record<string, string> = {},
  dry_run = true,
) {
  const { fields } = parseTaskForm(await fetchTaskFormHtml(session, ionTaskId, ionCustId))
  const newFields: Record<string, string> = { ...fields, ...changes }
  if (!newFields["LinkUsed"]) newFields["LinkUsed"] = "Save"
  if (!newFields["Submit"]) newFields["Submit"] = "Submit"
  const changed = Object.keys(changes)
    .filter((k) => fields[k] !== changes[k])
    .map((k) => ({ field: k, from: fields[k] ?? null, to: changes[k] }))

  if (dry_run) {
    return {
      dry_run: true, committed: false, ionTaskId: String(ionTaskId),
      would_post_to: `/tasks/addTask.cfm?EventID=${ionTaskId}&isIFrame=1`,
      changed, field_count: Object.keys(newFields).length, payload_preview: newFields,
    }
  }
  // BROWSER-FAITHFUL save: a real form submit is a plain document POST —
  // no X-Requested-With — and the Referer is the form page itself. The
  // first live boundary test (2026-08-08) showed the XHR-flavored POST
  // answered 200 with a page shell and saved NOTHING.
  const formUrl = `${session.ionOrigin}/tasks/addTask.cfm?EventID=${ionTaskId}&isIFrame=1`
  const res = await ionFetch(session, formUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "Referer": formUrl,
      "Origin": session.ionOrigin,
    },
    body: new URLSearchParams(newFields).toString(),
  })
  const txt = await res.text()
  return { dry_run: false, committed: res.ok, status: res.status, ionTaskId: String(ionTaskId), changed, response_preview: txt.slice(0, 3000) }
}

/**
 * Create a NEW task: prime the customer (the blank form binds to the primed
 * customer — same session mechanics as the UI's "add task from customer
 * tab"), serialize the blank form, overlay `fields`, POST without EventID.
 * dry_run=true (default) returns the exact payload WITHOUT submitting.
 * The echo (the response / next list read) carries the new EventID — the
 * caller records reality, never the prediction.
 */
export async function createTask(
  session: IonSession,
  ionCustId: string | number,
  fields: Record<string, string>,
  dry_run = true,
) {
  await ionFetchText(session, `${session.ionOrigin}/customers/customerTabs.cfm?customerid=${ionCustId}`)
  const blankHtml = await ionFetchText(session, `${session.ionOrigin}/tasks/addTask.cfm?isIFrame=1`)
  const blankForm = pickTaskForm(parse(blankHtml))
  if (!blankForm) throw new Error("blank addTask form not found — create path shape changed?")
  const blank = serializeForm(blankForm)
  const newFields: Record<string, string> = { ...blank, ...fields }
  delete newFields["EventID"]
  if (!newFields["LinkUsed"]) newFields["LinkUsed"] = "Save"
  if (!newFields["Submit"]) newFields["Submit"] = "Submit"

  if (dry_run) {
    return {
      dry_run: true, committed: false, ionCustId: String(ionCustId),
      would_post_to: "/tasks/addTask.cfm?isIFrame=1",
      blank_customer_binding: blank["CustomerID"] ?? "(no CustomerID field)",
      field_count: Object.keys(newFields).length, payload_preview: newFields,
    }
  }
  const res = await ionFetch(session, `${session.ionOrigin}/tasks/addTask.cfm?isIFrame=1`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
      "X-Requested-With": "XMLHttpRequest",
      "Referer": `${session.ionOrigin}/main.cfm`,
      "Origin": session.ionOrigin,
    },
    body: new URLSearchParams(newFields).toString(),
  })
  const txt = await res.text()
  // THE ECHO: the response (or its redirect) names the new task id. Try
  // the shapes we can anticipate; keep a generous preview either way so
  // the first live run teaches us the real one (echo over prediction).
  const idMatch = txt.match(/EventID["'\s]*[=:]["'\s]*(\d{5,})/i)
    ?? txt.match(/addTask\.cfm\?EventID=(\d{5,})/i)
    ?? txt.match(/taskid["'\s]*[=:]["'\s]*(\d{5,})/i)
  return {
    dry_run: false, committed: res.ok, status: res.status, ionCustId: String(ionCustId),
    new_event_id: idMatch ? idMatch[1] : null,
    response_preview: txt.slice(0, 1200),
  }
}

/**
 * DELETE a task — the task list's own mechanic (captured live 2026-08-09):
 * GET /tasks/tasklist.cfm?DeleteTaskID=<id> with the customer primed; the
 * UI confirm is client-side only. ION refuses deletion once a service log
 * exists, so this is inherently safe on never-served tasks. dry_run=true
 * (default) returns the URL it WOULD hit.
 */
export async function deleteTask(
  session: IonSession,
  ionTaskId: string | number,
  ionCustId: string | number,
  dry_run = true,
) {
  const url = `${session.ionOrigin}/tasks/tasklist.cfm?DeleteTaskID=${ionTaskId}`
  if (dry_run) {
    return { dry_run: true, committed: false, ionTaskId: String(ionTaskId), would_get: url }
  }
  if (ionCustId) await ionFetchText(session, `${session.ionOrigin}/customers/customerTabs.cfm?customerid=${ionCustId}`)
  const res = await ionFetch(session, url, { headers: { Referer: `${session.ionOrigin}/main.cfm` } })
  const txt = await res.text()
  // the echo: the returned task list should no longer contain the id
  const stillListed = txt.includes(`DeleteTaskID=${ionTaskId}`) || txt.includes(`EventID=${ionTaskId}`)
  return {
    dry_run: false, committed: res.ok && !stillListed, status: res.status,
    ionTaskId: String(ionTaskId), still_listed: stillListed,
    response_preview: txt.slice(0, 400),
  }
}

export function main() {
  return {
    library: "f/ION/_lib/task_detail",
    exports: ["fetchTaskFormHtml", "parseTaskForm", "getTaskDetail", "updateTask", "createTask", "deleteTask"],
  }
}
