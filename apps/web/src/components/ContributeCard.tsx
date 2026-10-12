"use client";

import { useState } from "react";
import LocationPicker, { type Picked } from "./LocationPicker";
import { type Lang, typeLabel } from "@/lib/i18n";
import { canSubmit, submit, type SubmissionKind } from "@/lib/submit";
import type { Site, SiteType } from "@/lib/types";

export type CardMode = "edit" | "new_site" | "feedback";

const TXT = {
  editTitle: ["Suggest an edit", "建议修改"], newTitle: ["Add a site", "添加遗迹"], fbTitle: ["Send feedback", "意见反馈"],
  editIntro: [
    "Correct a fact, the location, or add a detail or timeline event. Every change is checked and approved before it goes live.",
    "更正史实或位置，或补充细节与历史事件。所有修改都会经过核实和审批后才会发布。",
  ],
  newIntro: [
    "Know a historical church, school, hospital, cemetery or other place tied to Christian history? Pin it and tell us what it is.",
    "知道与基督教历史相关的教堂、学校、医院、墓地或其他地点吗？请标出位置并说明。",
  ],
  fbIntro: ["Ideas, problems, anything. We read everything.", "想法、问题，什么都可以。每条反馈我们都会看。"],
  what: ["What kind of change?", "修改类型"], kCorrection: ["Correct something", "更正错误"], kDetail: ["Add a detail or event", "补充细节或事件"],
  kLocation: ["Fix the location", "更正位置"],
  details: ["Details", "详细说明"], year: ["Year(s), if known", "年份（如知道）"],
  name: ["Name of the place", "地点名称"], type: ["Type", "类型"], history: ["What is it? Its history in brief", "这是什么地方？简述其历史"],
  source: ["Source: a link, book or archive (helps us verify)", "资料来源：链接、书籍或档案（便于核实）"],
  contact: ["Your email or name (optional, if we may follow up)", "您的邮箱或姓名（可选，便于联系）"],
  location: ["Location", "位置"], send: ["Send", "提交"], sending: ["Sending…", "提交中…"], cancel: ["Cancel", "取消"],
  thanks: ["Thank you! We'll check this and update the map once it's approved.", "谢谢！我们核实并审批后会更新地图。"],
  error: ["Couldn't send. Please try again.", "提交失败，请重试。"], unavailable: ["Submissions are offline right now.", "暂时无法提交。"],
  required: ["Please fill in the details.", "请填写详细说明。"],
} as const;
type K = keyof typeof TXT;
const TYPES: SiteType[] = ["church", "school", "hospital", "cemetery", "mission_station", "residence", "press", "orphanage", "other"];

export default function ContributeCard({ mode, site, lang, center, onClose }: {
  mode: CardMode; site?: Site | null; lang: Lang; center: [number, number]; onClose: () => void;
}) {
  const L = (k: K) => TXT[k][lang === "zh" ? 1 : 0];
  const [kind, setKind] = useState<"correction" | "new_detail" | "location">("correction");
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [type, setType] = useState<SiteType>("church");
  const [yearText, setYearText] = useState("");
  const [source, setSource] = useState("");
  const [contact, setContact] = useState("");
  const [pin, setPin] = useState<Picked | null>(null);
  const [state, setState] = useState<"idle" | "sending" | "done" | "error" | "invalid">("idle");

  if (!canSubmit) return <div className="card"><p>{L("unavailable")}</p><button onClick={onClose}>{L("cancel")}</button></div>;
  if (state === "done") return <div className="card done"><p>{L("thanks")}</p><button onClick={onClose}>OK</button></div>;

  const needsPin = mode === "new_site" || (mode === "edit" && kind === "location");
  const title = mode === "edit" ? L("editTitle") : mode === "new_site" ? L("newTitle") : L("fbTitle");
  const intro = mode === "edit" ? L("editIntro") : mode === "new_site" ? L("newIntro") : L("fbIntro");

  async function send() {
    if (!text.trim() || (mode === "new_site" && !name.trim())) { setState("invalid"); return; }
    setState("sending");
    const k: SubmissionKind = mode === "feedback" ? "feedback" : mode === "new_site" ? "new_site" : kind === "new_detail" ? "new_detail" : "correction";
    const payload: Record<string, unknown> = { text: text.slice(0, 4000), source: source.slice(0, 1000) || null, years: yearText.slice(0, 40) || null };
    if (mode === "new_site") Object.assign(payload, { name: name.slice(0, 200), site_type: type });
    if (mode === "edit" && site) Object.assign(payload, { site_slug: site.slug, site_name: site.nameEn, site_name_zh: site.nameZh, change: kind });
    if (needsPin && pin) Object.assign(payload, { lat: pin.lat, lng: pin.lng, located_by: pin.how, search_label: pin.label ?? null });
    try {
      await submit(k, { siteId: site?.id ?? null, lang, payload, contact });
      setState("done");
    } catch (e) {
      console.error(e);
      setState("error");
    }
  }

  return (
    <div className="card">
      <h3>{title}</h3>
      <p className="hint">{intro}</p>
      {mode === "edit" && (
        <div className="seg" role="radiogroup" aria-label={L("what")}>
          {(["correction", "new_detail", "location"] as const).map((v) => (
            <button key={v} type="button" role="radio" aria-checked={kind === v} onClick={() => setKind(v)}>
              {v === "correction" ? L("kCorrection") : v === "new_detail" ? L("kDetail") : L("kLocation")}
            </button>
          ))}
        </div>
      )}
      {mode === "new_site" && (
        <>
          <label>{L("name")}<input value={name} maxLength={200} onChange={(e) => setName(e.target.value)} /></label>
          <label>{L("type")}
            <select value={type} onChange={(e) => setType(e.target.value as SiteType)}>
              {TYPES.map((ty) => <option key={ty} value={ty}>{typeLabel(lang, ty)}</option>)}
            </select>
          </label>
        </>
      )}
      <label>{mode === "new_site" ? L("history") : L("details")}
        <textarea rows={4} value={text} maxLength={4000} onChange={(e) => setText(e.target.value)} />
      </label>
      {mode !== "feedback" && (
        <>
          <label>{L("year")}<input value={yearText} maxLength={40} onChange={(e) => setYearText(e.target.value)} /></label>
          <label>{L("source")}<input value={source} maxLength={1000} onChange={(e) => setSource(e.target.value)} /></label>
        </>
      )}
      {needsPin && (
        <div>
          <span className="label">{L("location")}</span>
          <LocationPicker lang={lang} center={site ? [site.lng, site.lat] : center} onChange={setPin} />
        </div>
      )}
      <label>{L("contact")}<input value={contact} maxLength={200} onChange={(e) => setContact(e.target.value)} /></label>
      {state === "invalid" && <p className="err">{L("required")}</p>}
      {state === "error" && <p className="err">{L("error")}</p>}
      <div className="row">
        <button className="primary" disabled={state === "sending"} onClick={send}>{state === "sending" ? L("sending") : L("send")}</button>
        <button onClick={onClose}>{L("cancel")}</button>
      </div>
    </div>
  );
}
