"use client";

import { useState } from "react";
import type { Lang } from "@/lib/i18n";
import { canSubmit, client, submit as sendSubmission } from "@/lib/submit";

const MAX_EDGE = 1600;

const TXT = {
  title: ["Add a photo of this site", "为此遗迹添加照片"],
  intro: [
    "Photos of the building, its grounds, plaques or old pictures you own help everyone see this history. They appear once reviewed and approved.",
    "建筑、院落、纪念牌，或您拥有的老照片，都能帮助大家看见这段历史。照片经审核批准后显示。",
  ],
  choose: ["Choose or take a photo", "选择或拍摄照片"],
  caption: ["Caption (what and where)", "说明（拍摄内容和位置）"],
  year: ["Year taken (optional)", "拍摄年份（可选）"],
  name: ["Your name for the credit (optional)", "署名（可选）"],
  consent: [
    "I took this photo or have the right to share it, and I license it under CC BY 4.0.",
    "这张照片由我拍摄或我有权分享，并以 CC BY 4.0 协议授权。",
  ],
  people: [
    "Please don't include identifiable people, especially at worship. Location data is removed automatically.",
    "请勿拍入可辨认的人，尤其是礼拜中的人。照片中的位置信息会被自动删除。",
  ],
  submit: ["Upload", "上传"], uploading: ["Uploading…", "上传中…"], cancel: ["Cancel", "取消"],
  thanks: ["Thank you! Your photo will appear here once it's approved.", "谢谢！照片审核批准后将显示在这里。"],
  error: ["Upload failed. Please try again.", "上传失败，请重试。"],
  unavailable: ["Photo uploads aren't available for this site yet.", "此遗迹暂不支持上传照片。"],
} as const;
type Key = keyof typeof TXT;

/** Re-encode through a canvas: resizes large photos and drops EXIF (including GPS). */
async function prepare(file: File): Promise<Blob> {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d")!.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("encode failed"))), "image/jpeg", 0.85));
}

export default function PhotoUpload({ siteId, lang, onClose }: { siteId: string | null; lang: Lang; onClose: () => void }) {
  const L = (k: Key) => TXT[k][lang === "zh" ? 1 : 0];
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [caption, setCaption] = useState("");
  const [year, setYear] = useState("");
  const [name, setName] = useState("");
  const [consent, setConsent] = useState(false);
  const [state, setState] = useState<"idle" | "uploading" | "done" | "error">("idle");

  if (!canSubmit || !siteId) return <div className="upload"><p>{L("unavailable")}</p><button onClick={onClose}>{L("cancel")}</button></div>;

  async function submit() {
    if (!file || !consent) return;
    setState("uploading");
    try {
      const db = client();
      const blob = await prepare(file);
      const photoId = crypto.randomUUID();
      const path = `pending/${siteId}/${crypto.randomUUID()}.jpg`;
      const up = await db.storage.from("photo-uploads").upload(path, blob, { contentType: "image/jpeg", upsert: false });
      if (up.error) throw up.error;
      const y = parseInt(year, 10);
      const row = await db.from("photos").insert({
        id: photoId, site_id: siteId, kind: "visitor", upload_path: path, license: "CC BY 4.0",
        caption: lang === "en" ? caption.slice(0, 500) || null : null,
        caption_zh: lang === "zh" ? caption.slice(0, 500) || null : null,
        year_taken: Number.isFinite(y) ? y : null, uploader_name: name.slice(0, 80) || null,
      });
      if (row.error) throw row.error;
      await sendSubmission("photo", { siteId, photoId, lang, payload: { caption: caption.slice(0, 500) || null, year_taken: Number.isFinite(y) ? y : null, upload_path: path } , contact: name });
      setState("done");
    } catch (e) {
      console.error(e);
      setState("error");
    }
  }

  if (state === "done") return <div className="upload done"><p>{L("thanks")}</p><button onClick={onClose}>OK</button></div>;

  return (
    <div className="upload">
      <h3>{L("title")}</h3>
      <p className="hint">{L("intro")}</p>
      <label className="pick">
        {preview ? <img src={preview} alt="" /> : <span>📷 {L("choose")}</span>}
        <input type="file" accept="image/*" onChange={(e) => {
          const f = e.target.files?.[0] ?? null;
          setFile(f);
          setPreview(f ? URL.createObjectURL(f) : null);
        }} />
      </label>
      <label>{L("caption")}<input value={caption} maxLength={500} onChange={(e) => setCaption(e.target.value)} /></label>
      <div className="row">
        <label>{L("year")}<input inputMode="numeric" value={year} maxLength={4} onChange={(e) => setYear(e.target.value.replace(/\D/g, ""))} /></label>
        <label>{L("name")}<input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} /></label>
      </div>
      <p className="hint">{L("people")}</p>
      <label className="consent"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} /> {L("consent")}</label>
      {state === "error" && <p className="err">{L("error")}</p>}
      <div className="row">
        <button className="primary" disabled={!file || !consent || state === "uploading"} onClick={submit}>
          {state === "uploading" ? L("uploading") : L("submit")}
        </button>
        <button onClick={onClose}>{L("cancel")}</button>
      </div>
    </div>
  );
}
