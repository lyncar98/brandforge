import { useState, useEffect } from "react";
import { api } from "../api";

export default function SpecPage() {
  const [value, setValue] = useState("");
  const [notice, setNotice] = useState<{ msg: string; type: string } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getSpec()
      .then((s) => { setValue(JSON.stringify(s, null, 2)); setLoading(false); })
      .catch((e) => { setNotice({ msg: String(e), type: "err" }); setLoading(false); });
  }, []);

  async function save() {
    setNotice(null);
    try {
      const parsed = JSON.parse(value);
      await api.putSpec(parsed);
      setNotice({ msg: "Spec saved. Go to Apply to converge drifted assets.", type: "ok" });
    } catch (e) {
      setNotice({ msg: String(e), type: "err" });
    }
  }

  async function reload() {
    setNotice(null);
    setLoading(true);
    try {
      const s = await api.getSpec();
      setValue(JSON.stringify(s, null, 2));
    } catch (e) {
      setNotice({ msg: String(e), type: "err" });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <div className="section-head" style={{ marginTop: 0 }}>
        <h2>Spec Editor</h2>
        <div className="section-actions">
          <button className="btn" onClick={reload}>Reload</button>
          <button className="btn btn-primary" onClick={save}>Save</button>
        </div>
      </div>

      {notice && (
        <div className={`notice notice-${notice.type}`}>{notice.msg}</div>
      )}

      <textarea
        className="spec-editor"
        value={loading ? "Loading…" : value}
        onChange={(e) => setValue(e.target.value)}
        spellCheck={false}
        disabled={loading}
      />

      <p className="spec-hint">
        Edit the brandspec JSON and click <strong>Save</strong>, then go to{" "}
        <strong>Apply</strong> to regenerate drifted assets.
      </p>
    </div>
  );
}
