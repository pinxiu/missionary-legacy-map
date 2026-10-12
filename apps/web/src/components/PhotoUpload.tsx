"use client";

import { useEffect, useState } from "react";
import type { Lang } from "@/lib/i18n";
import { canSubmit, client, submit as sendSubmission } from "@/lib/submit";

const MAX_EDGE = 1600;
const MAX_FILES = 12;

const TXT = {
  title: ["Add photos of this site", "为此遗迹添加照片"],
  intro: [
    "Photos of the building, its grounds, plaques or old pictures you own help everyone see this history. Add as many as you like (up to 12 at a time). They appear once reviewed and approved.",
    "建筑、院落、纪念牌，或您拥有的老照片，都能帮助大家看见这段历史。一次最多可添加12张，照片经审核批准后显示。",
  ],
  choose: ["Choose or take photos", "选择或拍摄照片"], more: ["+ Add more", "+ 继续添加"],
  caption: ["Caption (optional)", "说明（可选）"], remove: ["Remove", "移除"],
  year: ["Year taken (optional, applies to all)", "拍摄年份（可选，适用于全部）"],
  name: ["Your name for the credit (optional)", "署名（可选）"],
  consent: [
    "I took these photos or have the right to share them, and I license them under CC BY 4.0.",
    "这些照片由我拍摄或我有权分享，并以 CC BY 4.0 协议授权。",
  ],
  people: [
    "Please don't include identifiable people, especially at worship. Location data is removed automatically.",
    "请勿拍入可辨认的人，尤其是礼拜中的人。照片中的位置信息会被自动删除。",
  ],
  submit: ["Upload {n}", "上传 {n} 张"], uploading: ["Uploading {i} of {n}…", "正在上传 {i}/{n}…"], cancel: ["Cancel", "取消"],
  thanks: ["Thank you! Your photos will appear here once they're approved.", "谢谢！照片审核批准后将显示在这里。"],
  partial: ["{ok} of {n} uploaded; the rest failed. You can try those again.", "已上传 {ok}/{n} 张，其余失败，可再试一次。"],
  error: ["Upload failed. Please try again.", "上传失败，请重试。"],
  tooMany: ["Up to 12 photos at a time.", "一次最多12张。"],
  unavailable: ["Photo uploads aren't available for this site yet.", "此遗迹暂不支持上传照片。"],
} as const;
type Key = keyof typeof TXT;

type Item = { file: File; preview: string; caption: string };

/** Re-encode through a canvas: resizes large photos and drops EXIF (including GPS). */
async function prepare(file: File): Promise<Blob> {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d")!.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("encode failed"))), "image/jpeg", 0.85));
}

export default function PhotoUpload({ siteId, lang, onClose }: { siteId: string | null; lang: Lang; onClose: () => void }) {
  const L = (k: Key, vars: Record<string, number> = {}) =>
    Object.entries(vars).reduce<string>((s, [k2, v]) => s.replace(`{${k2}}`, String(v)), TXT[k][lang === "zh" ? 1 : 0]);
  const [items, setItems] = useState<Item[]>([]);
  const [year, setYear] = useState("");
  const [name, setName] = useState("");
  const [consent, setConsent] = useState(false);
  const [progress, setProgress] = useState(0);
  const [state, setState] = useState<"idle" | "uploading" | "done" | "partial" | "error" | "tooMany">("idle");
  const [okCount, setOkCount] = useState(0);

  useEffect(() => () => items.forEach((it) => URL.revokeObjectURL(it.preview)), []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!canSubmit || !siteId) return <div className="upload"><p>{L("unavailable")}</p><button onClick={onClose}>{L("cancel")}</button></div>;

  function addFiles(list: FileList | null) {
    if (!list) return;
    const incoming = [...list].filter((f) => f.type.startsWith("image/"));
    const room = MAX_FILES - items.length;
    if (incoming.length > room) setState("tooMany");
    else if (state === "tooMany") setState("idle");
    setItems((prev) => [...prev, ...incoming.slice(0, room).map((file) => ({ file, preview: URL.createObjectURL(file), caption: "" }))]);
  }

  function remove(i: number) {
    setItems((prev) => { URL.revokeObjectURL(prev[i].preview); return prev.filter((_, j) => j !== i); });
  }

  async function upload() {
    if (!items.length || !consent) return;
    setState("uploading");
    const db = client();
    const y = parseInt(year, 10);
    const yearTaken = Number.isFinite(y) ? y : null;
    const done: { photo_id: string; upload_path: string; caption: string | null }[] = [];
    const failed: Item[] = [];
    for (const [i, it] of items.entries()) {
      setProgress(i + 1);
      try {
        const blob = await prepare(it.file);
        const photoId = crypto.randomUUID();
        const path = `pending/${siteId}/${photoId}.jpg`;
        const up = await db.storage.from("photo-uploads").upload(path, blob, { contentType: "image/jpeg", upsert: false });
        if (up.error) throw up.error;
        const caption = it.caption.slice(0, 500) || null;
        const row = await db.from("photos").insert({
          id: photoId, site_id: siteId, kind: "visitor", upload_path: path, license: "CC BY 4.0",
          caption: lang === "en" ? caption : null, caption_zh: lang === "zh" ? caption : null,
          year_taken: yearTaken, uploader_name: name.slice(0, 80) || null,
        });
        if (row.error) throw row.error;
        done.push({ photo_id: photoId, upload_path: path, caption });
      } catch (e) {
        console.error(e);
        failed.push(it);
      }
    }
    if (done.length) {
      try {
        // One submission (and one review issue) for the whole batch.
        await sendSubmission("photo", {
          siteId, photoId: done[0].photo_id, lang, contact: name,
          payload: { photos: done, year_taken: yearTaken, caption: done[0].caption, upload_path: done[0].upload_path },
        });
      } catch (e) {
        console.error(e);
        setState("error");
        return;
      }
    }
    setOkCount(done.length);
    if (!failed.length) { setState("done"); return; }
    setItems(failed);
    setState(done.length ? "partial" : "error");
  }

  if (state === "done") return <div className="upload done"><p>{L("thanks")}</p><button onClick={onClose}>OK</button></div>;
  const busy = state === "uploading";

  return (
    <div className="upload">
      <h3>{L("title")}</h3>
      <p className="hint">{L("intro")}</p>

      {items.length > 0 && (
        <ul className="batch">
          {items.map((it, i) => (
            <li key={it.preview}>
              <img src={it.preview} alt="" />
              <div>
                <input placeholder={L("caption")} value={it.caption} maxLength={500} disabled={busy}
                  onChange={(e) => setItems((prev) => prev.map((p, j) => (j === i ? { ...p, caption: e.target.value } : p)))} />
                <button type="button" className="link" disabled={busy} onClick={() => remove(i)}>{L("remove")}</button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {items.length < MAX_FILES && (
        <label className={items.length ? "pick small" : "pick"}>
          <span>📷 {items.length ? L("more") : L("choose")}</span>
          <input type="file" accept="image/*" multiple disabled={busy}
            onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
        </label>
      )}
      {state === "tooMany" && <p className="err">{L("tooMany")}</p>}

      <div className="row">
        <label>{L("year")}<input inputMode="numeric" value={year} maxLength={4} onChange={(e) => setYear(e.target.value.replace(/\D/g, ""))} /></label>
        <label>{L("name")}<input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} /></label>
      </div>
      <p className="hint">{L("people")}</p>
      <label className="consent"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} /> {L("consent")}</label>
      {state === "partial" && <p className="err">{L("partial", { ok: okCount, n: okCount + items.length })}</p>}
      {state === "error" && <p className="err">{L("error")}</p>}
      <div className="row">
        <button className="primary" disabled={!items.length || !consent || busy} onClick={upload}>
          {busy ? L("uploading", { i: progress, n: items.length }) : L("submit", { n: items.length })}
        </button>
        <button onClick={onClose} disabled={busy}>{L("cancel")}</button>
      </div>
    </div>
  );
}
