"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";

type DocStatus = "queued" | "processing" | "indexed" | "error" | "duplicate" | "cancelled";
type SortBy = "uploaded" | "name" | "modified" | "document_date" | "category" | "doc_type" | "status";
type SortDir = "asc" | "desc";
type View = "chat" | "documents";
type DuplicateAction = "keep_this" | "keep_other" | "not_duplicate";

const ALL_STATUSES: DocStatus[] = ["queued", "processing", "indexed", "error", "duplicate", "cancelled"];
const PAGE_SIZES = [25, 50, 100, 200];

interface DocumentOut {
  id: string;
  original_filename: string;
  canonical_filename: string | null;
  category: string | null;
  doc_type: string | null;
  date: string | null;
  status: DocStatus;
  error: string | null;
  duplicate_similarity: number | null;
  quality_score: number | null;
  download_url: string | null;
  created_at: string;
  updated_at: string;
}

interface DocumentStats {
  total: number;
  indexed: number;
  queued: number;
  processing: number;
  error: number;
  duplicate: number;
  cancelled: number;
}

interface DuplicatePair {
  duplicate: DocumentOut;
  canonical: DocumentOut;
}

interface SourceDoc {
  document_id: string;
  canonical_filename: string | null;
  category: string | null;
  doc_type: string | null;
  date: string | null;
  download_url: string;
}

interface ChatTurn {
  role: "user" | "assistant";
  content: string;
  sources?: SourceDoc[];
}

interface ChatSessionSummary {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

interface ConfirmState {
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  danger?: boolean;
}

const POLL_STATUSES: DocStatus[] = ["queued", "processing"];

/** "2026-09-11 18:50" in the viewer's own local time — Date's getFullYear/
 * getMonth/getDate/getHours/getMinutes are always local-time in JS, so no
 * timezone conversion is needed here. */
function formatDateTime(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function downloadZip(documentIds: string[], filename = "documents.zip"): Promise<boolean> {
  const resp = await fetch("/api/documents/download-zip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document_ids: documentIds }),
  });
  if (!resp.ok) return false;
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return true;
}

/** Renders LLM-authored markdown (bold, lists, tables, etc). Single newlines
 * in the model's prose are treated as line breaks (remark-breaks) rather
 * than being collapsed per strict CommonMark, since that's closer to how
 * the model actually formats its answers. */
function MarkdownContent({ text }: { text: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{text}</ReactMarkdown>
    </div>
  );
}

/** A single source: plain link chip. Multiple sources: a compact dropdown
 * with a "download all" (.zip) action, so the bubble stays a fixed size no
 * matter how many documents were used to answer the question. */
function SourcesList({ sources }: { sources: SourceDoc[] }) {
  const [open, setOpen] = useState(false);
  const [zipping, setZipping] = useState(false);
  const [zipError, setZipError] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const handleDownloadAll = async () => {
    setZipping(true);
    setZipError(false);
    try {
      const ok = await downloadZip(sources.map((s) => s.document_id));
      if (!ok) setZipError(true);
    } finally {
      setZipping(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    const onClickAway = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClickAway);
    return () => document.removeEventListener("mousedown", onClickAway);
  }, [open]);

  if (sources.length === 1) {
    const s = sources[0];
    return (
      <div className="sources">
        <a className="source-chip" href={`/api${s.download_url}`} target="_blank" rel="noreferrer">
          📄 {s.canonical_filename ?? s.document_id.slice(0, 8)}
        </a>
      </div>
    );
  }

  return (
    <div className="sources-dropdown" ref={ref}>
      <button className="sources-toggle" onClick={() => setOpen((v) => !v)}>
        📎 {sources.length} sources {open ? "▴" : "▾"}
      </button>
      {open && (
        <div className="sources-menu">
          <button
            className="sources-menu-item sources-download-all"
            onClick={handleDownloadAll}
            disabled={zipping}
          >
            {zipping ? "⏳ Zipping…" : zipError ? "⚠ Failed — try again" : "⬇ Download all (.zip)"}
          </button>
          <div className="sources-menu-list">
            {sources.map((s) => (
              <a
                key={s.document_id}
                className="sources-menu-item"
                href={`/api${s.download_url}`}
                target="_blank"
                rel="noreferrer"
              >
                📄 {s.canonical_filename ?? s.document_id.slice(0, 8)}
              </a>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** Input + explicit Save/Cancel buttons. Enter saves, Escape cancels;
 * clicking away does neither — only the buttons (or Enter) commit. */
function InlineEdit({
  value,
  onChange,
  onSave,
  onCancel,
  error,
  listId,
  inputClassName,
}: {
  value: string;
  onChange: (v: string) => void;
  onSave: () => void;
  onCancel: () => void;
  error?: string | null;
  listId?: string;
  inputClassName?: string;
}) {
  return (
    <div>
      <div className="inline-edit-row">
        <input
          autoFocus
          className={`inline-edit-input ${inputClassName ?? ""}`}
          value={value}
          list={listId}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              onSave();
            }
            if (e.key === "Escape") {
              e.preventDefault();
              onCancel();
            }
          }}
        />
        <button className="icon-btn visible" title="Save" onClick={onSave}>
          ✓
        </button>
        <button className="icon-btn visible" title="Cancel" onClick={onCancel}>
          ✕
        </button>
      </div>
      {error && <div className="inline-edit-error">{error}</div>}
    </div>
  );
}

function DupeCard({ label, doc, kept }: { label: string; doc: DocumentOut; kept: boolean }) {
  const name = doc.canonical_filename ?? doc.original_filename;
  return (
    <div className={`dupe-card ${kept ? "picked" : ""}`}>
      <div className="dupe-card-label">{label}</div>
      <div className="dupe-card-name">{name}</div>
      <dl>
        <dt>Category</dt>
        <dd>{doc.category ?? "—"}</dd>
        <dt>Doc date</dt>
        <dd>{doc.date ?? "—"}</dd>
        <dt>Uploaded</dt>
        <dd>{formatDateTime(doc.created_at)}</dd>
        <dt>Quality score</dt>
        <dd>{doc.quality_score ?? "—"}</dd>
      </dl>
    </div>
  );
}

export default function Home() {
  const [view, setView] = useState<View>("chat");
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  // Chat history sidebar collapses into an off-canvas drawer below the
  // mobile breakpoint (see globals.css) — closed by default there so it
  // doesn't cover the whole screen on load.
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // ---------------- chat state ----------------
  const [chats, setChats] = useState<ChatSessionSummary[]>([]);
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [statusLine, setStatusLine] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // ---------------- chat: voice input ----------------
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [micNotice, setMicNotice] = useState<string | null>(null);
  // null while the first check is in flight — the button stays disabled
  // until we actually know, rather than flashing enabled-then-disabled.
  const [speechAvailable, setSpeechAvailable] = useState<boolean | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);

  // ---------------- documents state ----------------
  const [documents, setDocuments] = useState<DocumentOut[]>([]);
  const [documentsTotal, setDocumentsTotal] = useState(0);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [categories, setCategories] = useState<string[]>([]);
  const [sortBy, setSortBy] = useState<SortBy>("uploaded");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadNotice, setUploadNotice] = useState<string | null>(null);

  const [duplicates, setDuplicates] = useState<DuplicatePair[]>([]);
  const [showDuplicates, setShowDuplicates] = useState(false);

  const [stats, setStats] = useState<DocumentStats | null>(null);
  const [showQueuePopup, setShowQueuePopup] = useState(false);
  const [showErrorsPopup, setShowErrorsPopup] = useState(false);
  const [queueDocs, setQueueDocs] = useState<DocumentOut[]>([]);
  const [errorDocs, setErrorDocs] = useState<DocumentOut[]>([]);

  const askConfirm = (message: string, onConfirm: () => void, confirmLabel = "Delete", danger = true) =>
    setConfirm({ message, onConfirm, confirmLabel, danger });

  // ---------------- chat: data loading ----------------

  const fetchChats = useCallback(async () => {
    const resp = await fetch("/api/chats", { cache: "no-store" });
    if (resp.ok) setChats(await resp.json());
  }, []);

  useEffect(() => {
    fetchChats();
  }, [fetchChats]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, statusLine]);

  const startNewChat = () => {
    setActiveChatId(null);
    setMessages([]);
  };

  const loadChat = async (id: string) => {
    if (id === activeChatId || sending) return;
    const resp = await fetch(`/api/chats/${id}`, { cache: "no-store" });
    if (!resp.ok) return;
    const data = await resp.json();
    setActiveChatId(id);
    setMessages(
      (data.messages ?? []).map((m: any) => ({
        role: m.role,
        content: m.content,
        sources: m.sources && m.sources.length > 0 ? m.sources : undefined,
      }))
    );
  };

  const deleteChat = (id: string) => {
    askConfirm("Delete this chat? This can't be undone.", async () => {
      await fetch(`/api/chats/${id}`, { method: "DELETE" });
      if (id === activeChatId) {
        setActiveChatId(null);
        setMessages([]);
      }
      fetchChats();
    });
  };

  // ---------------- chat: sending ----------------

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || sending) return;

    setMessages((prev) => [...prev, { role: "user", content: text }, { role: "assistant", content: "" }]);
    setInput("");
    setSending(true);
    setStatusLine("Thinking…");

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: activeChatId, message: text }),
      });
      if (!resp.body) throw new Error("no response stream");

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      const applyToLastAssistant = (fn: (m: ChatTurn) => ChatTurn) => {
        setMessages((prev) => {
          const next = [...prev];
          const idx = next.length - 1;
          next[idx] = fn(next[idx]);
          return next;
        });
      };

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() ?? "";

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data:")) continue;
          const jsonStr = line.slice(5).trim();
          if (!jsonStr) continue;

          let evt: any;
          try {
            evt = JSON.parse(jsonStr);
          } catch {
            continue;
          }

          if (evt.type === "chat_id") {
            setActiveChatId(evt.chat_id);
            fetchChats();
          } else if (evt.type === "token") {
            setStatusLine(null);
            applyToLastAssistant((m) => ({ ...m, content: m.content + evt.text }));
          } else if (evt.type === "status") {
            setStatusLine(evt.text);
          } else if (evt.type === "sources") {
            applyToLastAssistant((m) => ({ ...m, sources: evt.documents }));
          } else if (evt.type === "error") {
            setStatusLine(null);
            applyToLastAssistant((m) => ({ ...m, content: m.content + `\n\n⚠️ ${evt.text}` }));
          } else if (evt.type === "done") {
            setStatusLine(null);
          }
        }
      }

      fetchChats();
    } catch (err: any) {
      setStatusLine(null);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `⚠️ Failed to reach the backend: ${err?.message ?? err}` },
      ]);
    } finally {
      setSending(false);
    }
  };

  // ---------------- chat: voice input ----------------

  useEffect(() => {
    if (!micNotice) return;
    const t = setTimeout(() => setMicNotice(null), 8000);
    return () => clearTimeout(t);
  }, [micNotice]);

  // The speech service is an optional, separately-run host process (see
  // README) — poll its reachability so the mic button reflects reality
  // instead of only failing on the first click.
  useEffect(() => {
    let cancelled = false;
    const checkSpeechStatus = async () => {
      try {
        const resp = await fetch("/api/speech/status", { cache: "no-store" });
        const data = await resp.json().catch(() => null);
        if (!cancelled) setSpeechAvailable(Boolean(data?.available));
      } catch {
        if (!cancelled) setSpeechAvailable(false);
      }
    };
    checkSpeechStatus();
    const interval = setInterval(checkSpeechStatus, 30000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const transcribeAudio = async (blob: Blob) => {
    setTranscribing(true);
    try {
      const form = new FormData();
      form.append("audio", blob, "clip.webm");
      const resp = await fetch("/api/speech/transcribe", { method: "POST", body: form });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) {
        setMicNotice(`⚠️ Voice input unavailable: ${data?.detail ?? resp.status}`);
        return;
      }
      const text = (data?.text ?? "").trim();
      if (!text) {
        setMicNotice("Didn't catch any speech — try again.");
        return;
      }
      // Append rather than replace, so dictating doesn't wipe out anything
      // already typed — and never auto-send, since misheard names/amounts
      // matter a lot for questions about the user's own documents.
      setInput((prev) => (prev.trim() ? `${prev.trim()} ${text}` : text));
    } catch (err: any) {
      setMicNotice(`⚠️ Voice input unavailable: ${err?.message ?? err}`);
    } finally {
      setTranscribing(false);
    }
  };

  const toggleRecording = async () => {
    if (recording) {
      mediaRecorderRef.current?.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";
      const recorder = new MediaRecorder(stream, { mimeType });
      audioChunksRef.current = [];

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunksRef.current.push(e.data);
      };
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        const blob = new Blob(audioChunksRef.current, { type: mimeType });
        transcribeAudio(blob);
      };

      mediaRecorderRef.current = recorder;
      recorder.start();
      setRecording(true);
    } catch (err: any) {
      if (err?.name === "NotAllowedError" || err?.name === "SecurityError") {
        setMicNotice("Microphone access denied — allow microphone permission for this site in your browser settings.");
      } else if (err?.name === "NotFoundError") {
        setMicNotice("No microphone found.");
      } else {
        setMicNotice(`Couldn't start recording: ${err?.message ?? err}`);
      }
    }
  };

  // ---------------- documents: data loading ----------------

  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput), 350);
    return () => clearTimeout(t);
  }, [searchInput]);

  // Any filter/sort change invalidates the current page.
  useEffect(() => {
    setPage(0);
  }, [search, categoryFilter, statusFilter, sortBy, sortDir]);

  const fetchDocuments = useCallback(async () => {
    const params = new URLSearchParams();
    if (search.trim()) params.set("q", search.trim());
    if (categoryFilter) params.set("category", categoryFilter);
    if (statusFilter) params.set("status", statusFilter);
    params.set("sort_by", sortBy);
    params.set("sort_dir", sortDir);
    params.set("limit", String(pageSize));
    params.set("offset", String(page * pageSize));
    const resp = await fetch(`/api/documents?${params.toString()}`, { cache: "no-store" });
    if (resp.ok) {
      const data = await resp.json();
      setDocuments(data.items ?? []);
      setDocumentsTotal(data.total ?? 0);
    }
  }, [search, categoryFilter, statusFilter, sortBy, sortDir, page, pageSize]);

  useEffect(() => {
    fetchDocuments();
  }, [fetchDocuments]);

  const fetchCategories = useCallback(async () => {
    const resp = await fetch("/api/documents/categories", { cache: "no-store" });
    if (resp.ok) setCategories(await resp.json());
  }, []);

  useEffect(() => {
    fetchCategories();
  }, [fetchCategories]);

  const fetchStats = useCallback(async () => {
    const resp = await fetch("/api/documents/stats", { cache: "no-store" });
    if (resp.ok) setStats(await resp.json());
  }, []);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  const fetchDuplicates = useCallback(async () => {
    const resp = await fetch("/api/documents/duplicates", { cache: "no-store" });
    if (resp.ok) setDuplicates(await resp.json());
  }, []);

  useEffect(() => {
    fetchDuplicates();
  }, [fetchDuplicates]);

  // Global (unfiltered) pending count drives polling, not whatever happens
  // to be on the current filtered/paginated page — otherwise a filtered view
  // with nothing pending in it would never notice unrelated documents
  // finishing processing elsewhere.
  useEffect(() => {
    const hasPending = !!stats && stats.queued + stats.processing > 0;
    if (!hasPending) return;
    const t = setInterval(() => {
      fetchDocuments();
      fetchDuplicates();
      fetchStats();
      fetchCategories();
    }, 4000);
    return () => clearInterval(t);
  }, [stats, fetchDocuments, fetchDuplicates, fetchStats, fetchCategories]);

  const resolveDuplicate = async (pair: DuplicatePair, action: DuplicateAction) => {
    await fetch(`/api/documents/${pair.duplicate.id}/resolve-duplicate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    fetchDuplicates();
    fetchDocuments();
    fetchStats();
  };

  useEffect(() => {
    if (!uploadNotice) return;
    const t = setTimeout(() => setUploadNotice(null), 10000);
    return () => clearTimeout(t);
  }, [uploadNotice]);

  const uploadFiles = useCallback(
    async (files: FileList | File[]) => {
      const form = new FormData();
      for (const f of Array.from(files)) form.append("files", f);

      try {
        // Straight to the backend, not through the Next.js proxy: for large
        // batches, buffering the whole multipart body in the Node process
        // just to re-encode and forward it doesn't scale (seen it fail
        // around ~70 scanned files). FastAPI/Starlette streams multipart
        // uploads to disk instead of buffering them all in memory.
        const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";
        const resp = await fetch(`${backendUrl}/documents`, { method: "POST", body: form });

        if (resp.ok) {
          const results: { original_filename: string; status: string; detail?: string | null }[] =
            await resp.json();
          const duplicates = results.filter((r) => r.status === "duplicate").map((r) => r.original_filename);
          const tooLarge = results.filter((r) => r.status === "too_large").map((r) => `${r.original_filename} (${r.detail})`);

          const notices: string[] = [];
          if (duplicates.length > 0) {
            notices.push(
              `Already have ${duplicates.length === 1 ? "this file" : "these files"} (identical content, skipped): ${duplicates.join(", ")}`
            );
          }
          if (tooLarge.length > 0) {
            notices.push(`Too large, skipped: ${tooLarge.join(", ")}`);
          }
          if (notices.length > 0) setUploadNotice(notices.join(" — "));
        } else {
          setUploadNotice(`Upload failed (${resp.status}). Check the backend logs.`);
        }
      } catch (err: any) {
        setUploadNotice(`Upload failed: ${err?.message ?? err}`);
      }

      fetchDocuments();
      fetchStats();
      fetchCategories();
    },
    [fetchDocuments, fetchStats, fetchCategories]
  );

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files?.length) uploadFiles(e.dataTransfer.files);
  };

  // ---------------- documents: sort / select ----------------

  const handleSort = (col: SortBy) => {
    if (col === sortBy) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortBy(col);
      setSortDir(col === "name" ? "asc" : "desc");
    }
  };

  const sortIndicator = (col: SortBy) => (sortBy === col ? (sortDir === "asc" ? " ▲" : " ▼") : "");

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    setSelectedIds((prev) => (prev.size === documents.length ? new Set() : new Set(documents.map((d) => d.id))));
  };

  // ---------------- documents: rename ----------------

  const startRename = (doc: DocumentOut) => {
    setRenamingId(doc.id);
    setRenameValue(doc.canonical_filename ?? doc.original_filename);
    setRenameError(null);
  };

  const cancelRename = () => {
    setRenamingId(null);
    setRenameError(null);
  };

  const commitRename = async (doc: DocumentOut) => {
    const value = renameValue.trim();
    const current = doc.canonical_filename ?? doc.original_filename;
    if (!value || value === current) {
      cancelRename();
      return;
    }
    const resp = await fetch(`/api/documents/${doc.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: value }),
    });
    if (resp.ok) {
      cancelRename();
      fetchDocuments();
    } else {
      const err = await resp.json().catch(() => null);
      setRenameError(err?.detail ?? "Rename failed");
    }
  };

  // ---------------- documents: reprocess / delete / cancel ----------------

  const reprocessDocument = (doc: DocumentOut) => {
    askConfirm(
      `Re-scan "${doc.canonical_filename ?? doc.original_filename}"? This re-runs OCR and extraction from the file already on disk and replaces its current category/dates/facts/line items.`,
      async () => {
        const resp = await fetch(`/api/documents/${doc.id}/reprocess`, { method: "POST" });
        if (!resp.ok) {
          const err = await resp.json().catch(() => null);
          setUploadNotice(`Reprocess failed: ${err?.detail ?? resp.status}`);
        }
        fetchDocuments();
        fetchStats();
      },
      "Re-scan",
      false
    );
  };

  const deleteDocument = (doc: DocumentOut) => {
    askConfirm(
      `Delete "${doc.canonical_filename ?? doc.original_filename}"? This removes the file and its indexed data permanently.`,
      async () => {
        await fetch(`/api/documents/${doc.id}`, { method: "DELETE" });
        setSelectedIds((prev) => {
          const next = new Set(prev);
          next.delete(doc.id);
          return next;
        });
        fetchDocuments();
        fetchDuplicates();
        fetchStats();
      }
    );
  };

  const cancelDocuments = (ids: string[], { refreshPopup = false }: { refreshPopup?: boolean } = {}) => {
    if (ids.length === 0) return;
    askConfirm(
      `Cancel ${ids.length} queued document${ids.length === 1 ? "" : "s"}? They'll stay unprocessed until re-scanned.`,
      async () => {
        await fetch("/api/documents/bulk-cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ document_ids: ids }),
        });
        setSelectedIds((prev) => {
          const next = new Set(prev);
          ids.forEach((id) => next.delete(id));
          return next;
        });
        fetchDocuments();
        fetchStats();
        if (refreshPopup) openQueuePopup();
      },
      "Cancel",
      true
    );
  };

  const bulkDelete = () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    askConfirm(
      `Delete ${ids.length} selected document${ids.length === 1 ? "" : "s"}? This removes the files and their indexed data permanently.`,
      async () => {
        await fetch("/api/documents/bulk-delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ document_ids: ids }),
        });
        setSelectedIds(new Set());
        fetchDocuments();
        fetchDuplicates();
        fetchStats();
      }
    );
  };

  const bulkReprocess = (ids: string[] = Array.from(selectedIds), refreshPopup = false) => {
    if (ids.length === 0) return;
    askConfirm(
      `Re-scan ${ids.length} selected document${ids.length === 1 ? "" : "s"}? This re-runs OCR and extraction from the files already on disk and replaces their current category/dates/facts/line items.`,
      async () => {
        await fetch("/api/documents/bulk-reprocess", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ document_ids: ids }),
        });
        setSelectedIds(new Set());
        fetchDocuments();
        fetchStats();
        if (refreshPopup) openErrorsPopup();
      },
      "Re-scan",
      false
    );
  };

  // ---------------- documents: processing/queue + errors popups ----------------

  const openQueuePopup = useCallback(async () => {
    setShowQueuePopup(true);
    const [pResp, qResp] = await Promise.all([
      fetch("/api/documents?status=processing&sort_by=uploaded&sort_dir=asc&limit=500", { cache: "no-store" }),
      fetch("/api/documents?status=queued&sort_by=uploaded&sort_dir=asc&limit=500", { cache: "no-store" }),
    ]);
    const p = pResp.ok ? (await pResp.json()).items ?? [] : [];
    const q = qResp.ok ? (await qResp.json()).items ?? [] : [];
    setQueueDocs([...p, ...q]);
  }, []);

  const openErrorsPopup = useCallback(async () => {
    setShowErrorsPopup(true);
    const resp = await fetch("/api/documents?status=error&sort_by=uploaded&sort_dir=desc&limit=500", {
      cache: "no-store",
    });
    if (resp.ok) setErrorDocs((await resp.json()).items ?? []);
  }, []);

  // The stats-bar counts refresh on their own polling loop, but the popup
  // bodies were only ever fetched once at open time — a document finishing
  // or moving from queued to processing while the popup sits open wouldn't
  // show up until the user manually triggered another action. Poll the open
  // popup the same way.
  useEffect(() => {
    if (!showQueuePopup) return;
    const t = setInterval(openQueuePopup, 3000);
    return () => clearInterval(t);
  }, [showQueuePopup, openQueuePopup]);

  useEffect(() => {
    if (!showErrorsPopup) return;
    const t = setInterval(openErrorsPopup, 3000);
    return () => clearInterval(t);
  }, [showErrorsPopup, openErrorsPopup]);

  const totalPages = Math.max(1, Math.ceil(documentsTotal / pageSize));
  const rangeStart = documentsTotal === 0 ? 0 : page * pageSize + 1;
  const rangeEnd = Math.min(documentsTotal, page * pageSize + documents.length);

  return (
    <div className="app-shell">
      <div className="topbar">
        {view === "chat" && (
          <button
            className="icon-btn visible sidebar-toggle"
            title="Chat history"
            onClick={() => setSidebarOpen((v) => !v)}
          >
            ☰
          </button>
        )}
        <div className="brand">
          <strong>📁 Document Storage</strong>
          <span>Local OCR + RAG over your files</span>
        </div>
        <div className="tabs">
          <button className={`tab-btn ${view === "chat" ? "active" : ""}`} onClick={() => setView("chat")}>
            💬 Chat
          </button>
          <button className={`tab-btn ${view === "documents" ? "active" : ""}`} onClick={() => setView("documents")}>
            📁 Documents
          </button>
        </div>

        <div className="spacer" />

        {stats && (
          <div className="stats-bar">
            <span className="stat-item">{stats.total} total</span>
            <span className="stat-item">{stats.indexed} indexed</span>
            <button
              className={`stat-item stat-chip ${stats.queued + stats.processing > 0 ? "clickable" : "disabled"}`}
              onClick={() => stats.queued + stats.processing > 0 && openQueuePopup()}
              title="Documents currently processing or waiting in the queue"
            >
              ⏳ {stats.queued + stats.processing} processing/queued
            </button>
            <button
              className={`stat-item stat-chip ${stats.error > 0 ? "clickable warn" : "disabled"}`}
              onClick={() => stats.error > 0 && openErrorsPopup()}
              title="Documents that failed to process"
            >
              ⚠ {stats.error} error{stats.error === 1 ? "" : "s"}
            </button>
          </div>
        )}
      </div>

      <div className="app-body">
        {view === "chat" ? (
          <>
            <div
              className={`sidebar-backdrop ${sidebarOpen ? "visible" : ""}`}
              onClick={() => setSidebarOpen(false)}
            />
            <aside className={`chat-sidebar ${sidebarOpen ? "open" : ""}`}>
              <div className="sidebar-mobile-header">
                <span>Chats</span>
                <button className="icon-btn visible" title="Close" onClick={() => setSidebarOpen(false)}>
                  ✕
                </button>
              </div>
              <button
                className="new-chat-btn"
                onClick={() => {
                  startNewChat();
                  setSidebarOpen(false);
                }}
              >
                + New chat
              </button>
              <div className="chat-history">
                {chats.length === 0 && <div className="empty-hint">No chats yet</div>}
                {chats.map((c) => (
                  <div
                    key={c.id}
                    className={`chat-history-row ${c.id === activeChatId ? "active" : ""}`}
                    onClick={() => {
                      loadChat(c.id);
                      setSidebarOpen(false);
                    }}
                  >
                    <span className="chat-history-title">{c.title || "New chat"}</span>
                    <button
                      className="icon-btn"
                      title="Delete chat"
                      onClick={(e) => {
                        e.stopPropagation();
                        deleteChat(c.id);
                      }}
                    >
                      🗑
                    </button>
                  </div>
                ))}
              </div>
            </aside>

            <main className="chat">
              <div className="chat-messages">
                {messages.length === 0 && (
                  <div className="empty-state">
                    <h2>Ask about your documents</h2>
                    <p>
                      e.g. "What did I spend on auto repairs in 2026?", "What is my SIN number?", "Where did I
                      work in 2024?"
                    </p>
                  </div>
                )}

                {messages.map((m, i) => (
                  <div key={i} className={`msg msg-${m.role}`}>
                    {m.role === "assistant" ? (
                      m.content ? (
                        <MarkdownContent text={m.content} />
                      ) : sending && i === messages.length - 1 ? (
                        "…"
                      ) : (
                        ""
                      )
                    ) : (
                      m.content
                    )}
                    {m.sources && m.sources.length > 0 && <SourcesList sources={m.sources} />}
                  </div>
                ))}

                {statusLine && <div className="msg-status">{statusLine}</div>}
                <div ref={messagesEndRef} />
              </div>

              {micNotice && <div className="mic-notice">{micNotice}</div>}

              <div className="chat-input-bar">
                <textarea
                  rows={1}
                  placeholder="Ask about your documents…"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      sendMessage();
                    }
                  }}
                />
                <button
                  className={`mic-btn ${recording ? "recording" : ""}`}
                  title={
                    speechAvailable === false
                      ? "Voice input unavailable — speech service isn't running"
                      : recording
                      ? "Stop recording"
                      : "Speak your question"
                  }
                  onClick={toggleRecording}
                  disabled={sending || (transcribing && !recording) || !speechAvailable}
                >
                  {recording ? "⏹" : transcribing ? "…" : "🎤"}
                </button>
                <button onClick={sendMessage} disabled={sending || !input.trim()}>
                  Send
                </button>
              </div>
            </main>
          </>
        ) : (
          <main className="documents-panel">
            <div className="doc-toolbar">
              <input
                className="search-input"
                placeholder="Search documents…"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
              />

              <select
                className="filter-select"
                value={categoryFilter}
                onChange={(e) => setCategoryFilter(e.target.value)}
              >
                <option value="">All categories</option>
                {categories.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>

              <select className="filter-select" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="">All statuses</option>
                {ALL_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>

              <div
                className={`upload-btn ${dragging ? "dragging" : ""}`}
                onClick={() => fileInputRef.current?.click()}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragging(true);
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={onDrop}
              >
                ⬆ Upload / drop files
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  hidden
                  onChange={(e) => e.target.files && uploadFiles(e.target.files)}
                />
              </div>

              <div className="spacer" />

              {duplicates.length > 0 && (
                <button className="dupe-btn" onClick={() => setShowDuplicates(true)}>
                  ⚠ {duplicates.length} possible duplicate{duplicates.length === 1 ? "" : "s"}
                </button>
              )}

              {selectedIds.size > 0 && (
                <>
                  <button className="btn-secondary" onClick={() => bulkReprocess()}>
                    ♻️ Re-scan {selectedIds.size} selected
                  </button>
                  <button className="btn-danger" onClick={bulkDelete}>
                    Delete {selectedIds.size} selected
                  </button>
                </>
              )}
            </div>

            {uploadNotice && (
              <div className="upload-notice">
                {uploadNotice}
                <button className="icon-btn visible" title="Dismiss" onClick={() => setUploadNotice(null)}>
                  ✕
                </button>
              </div>
            )}

            <div className="doc-table-wrap">
              <table className="doc-table">
                <thead>
                  <tr>
                    <th style={{ width: 32 }}>
                      <input
                        type="checkbox"
                        checked={documents.length > 0 && selectedIds.size === documents.length}
                        onChange={toggleSelectAll}
                      />
                    </th>
                    <th className="sortable" onClick={() => handleSort("name")}>
                      Name{sortIndicator("name")}
                    </th>
                    <th className="sortable" onClick={() => handleSort("category")}>
                      Category{sortIndicator("category")}
                    </th>
                    <th className="sortable col-type" onClick={() => handleSort("doc_type")}>
                      Type{sortIndicator("doc_type")}
                    </th>
                    <th className="sortable col-doc-date" onClick={() => handleSort("document_date")}>
                      Document date{sortIndicator("document_date")}
                    </th>
                    <th className="sortable col-modified" onClick={() => handleSort("modified")}>
                      Modified{sortIndicator("modified")}
                    </th>
                    <th className="sortable col-uploaded" onClick={() => handleSort("uploaded")}>
                      Uploaded{sortIndicator("uploaded")}
                    </th>
                    <th className="sortable" onClick={() => handleSort("status")}>
                      Status{sortIndicator("status")}
                    </th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {documents.length === 0 && (
                    <tr className="empty-table-row">
                      <td colSpan={9}>No documents match your search.</td>
                    </tr>
                  )}
                  {documents.map((d) => {
                    const displayName = d.canonical_filename ?? d.original_filename;
                    return (
                      <tr key={d.id}>
                        <td>
                          <input type="checkbox" checked={selectedIds.has(d.id)} onChange={() => toggleSelect(d.id)} />
                        </td>
                        <td>
                          {renamingId === d.id ? (
                            <InlineEdit
                              value={renameValue}
                              onChange={setRenameValue}
                              onSave={() => commitRename(d)}
                              onCancel={cancelRename}
                              error={renameError}
                            />
                          ) : (
                            <div className="doc-name-cell" title={d.error ?? undefined}>
                              <div className="doc-name-main">
                                <span className="doc-name-text">{displayName}</span>
                                {d.original_filename !== displayName && (
                                  <span className="doc-original-name" title={d.original_filename}>
                                    {d.original_filename}
                                  </span>
                                )}
                              </div>
                              <button
                                className="icon-btn visible"
                                title="Rename"
                                onClick={() => startRename(d)}
                              >
                                ✏️
                              </button>
                            </div>
                          )}
                        </td>
                        <td>{d.category ? <span className="badge">{d.category}</span> : "—"}</td>
                        <td className="col-type">{d.doc_type ?? "—"}</td>
                        <td className="col-doc-date">{d.date ?? "—"}</td>
                        <td className="col-modified">{formatDateTime(d.updated_at)}</td>
                        <td className="col-uploaded">{formatDateTime(d.created_at)}</td>
                        <td>
                          <span className="status-pill">
                            <span className={`status-dot status-${d.status}`} />
                            {d.status}
                          </span>
                        </td>
                        <td>
                          <div className="row-actions">
                            {d.status === "indexed" && d.download_url && (
                              <a
                                className="icon-btn visible"
                                title="Download"
                                href={`/api${d.download_url}`}
                                target="_blank"
                                rel="noreferrer"
                              >
                                ⬇
                              </a>
                            )}
                            {d.status === "queued" && (
                              <button
                                className="icon-btn visible"
                                title="Cancel (still queued)"
                                onClick={() => cancelDocuments([d.id])}
                              >
                                ⛔
                              </button>
                            )}
                            <button
                              className="icon-btn visible"
                              title="Re-scan (re-run OCR + extraction)"
                              onClick={() => reprocessDocument(d)}
                            >
                              ♻️
                            </button>
                            <button className="icon-btn visible" title="Delete" onClick={() => deleteDocument(d)}>
                              🗑
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="pagination-bar">
              <span className="pagination-range">
                {documentsTotal === 0
                  ? "No documents"
                  : `Showing ${rangeStart}-${rangeEnd} of ${documentsTotal}`}
              </span>
              <div className="pagination-controls">
                <select
                  className="filter-select"
                  value={pageSize}
                  onChange={(e) => setPageSize(Number(e.target.value))}
                >
                  {PAGE_SIZES.map((n) => (
                    <option key={n} value={n}>
                      {n} / page
                    </option>
                  ))}
                </select>
                <button className="btn-secondary" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>
                  ‹ Prev
                </button>
                <span className="pagination-page">
                  Page {page + 1} of {totalPages}
                </span>
                <button
                  className="btn-secondary"
                  disabled={page + 1 >= totalPages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next ›
                </button>
              </div>
            </div>
          </main>
        )}
      </div>

      {showDuplicates && (
        <div className="confirm-overlay" onClick={() => setShowDuplicates(false)}>
          <div className="dupe-modal" onClick={(e) => e.stopPropagation()}>
            <h2>Possible duplicates</h2>
            <p>
              Grading picks a "kept" copy automatically (richer OCR text, more extracted fields); the other is
              suppressed from search/chat/totals but not deleted. Review and override below if it picked wrong.
            </p>

            {duplicates.length === 0 && <p>Nothing left to review.</p>}

            {duplicates.map((pair) => (
              <div className="dupe-pair" key={pair.duplicate.id}>
                <div className="dupe-cards">
                  <DupeCard label="Currently kept" doc={pair.canonical} kept />
                  <DupeCard label="Suppressed (duplicate)" doc={pair.duplicate} kept={false} />
                </div>
                {pair.duplicate.duplicate_similarity != null && (
                  <p style={{ margin: "0 0 10px", fontSize: 12, color: "var(--muted)" }}>
                    ~{Math.round(pair.duplicate.duplicate_similarity * 100)}% similar
                  </p>
                )}
                <div className="dupe-actions">
                  <button onClick={() => resolveDuplicate(pair, "keep_other")}>✓ Confirm current pick</button>
                  <button onClick={() => resolveDuplicate(pair, "keep_this")}>⇄ Keep the other one instead</button>
                  <button onClick={() => resolveDuplicate(pair, "not_duplicate")}>Not a duplicate — keep both</button>
                </div>
              </div>
            ))}

            <div className="confirm-actions">
              <button className="btn-secondary" onClick={() => setShowDuplicates(false)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {showQueuePopup && (
        <div className="confirm-overlay" onClick={() => setShowQueuePopup(false)}>
          <div className="dupe-modal" onClick={(e) => e.stopPropagation()}>
            <h2>Processing &amp; queue</h2>
            <p>
              Documents currently being OCR'd/extracted, and documents waiting their turn (in the order they'll
              run). A document already processing can't be safely interrupted, so only queued ones can be
              cancelled.
            </p>

            {queueDocs.length === 0 && <p>Nothing processing or queued right now.</p>}

            {queueDocs.length > 0 && (
              <table className="popup-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Name</th>
                    <th>Status</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {queueDocs.map((d, i) => (
                    <tr key={d.id}>
                      <td>{i + 1}</td>
                      <td className="popup-name-cell" title={d.original_filename}>
                        {d.canonical_filename ?? d.original_filename}
                      </td>
                      <td>
                        <span className="status-pill">
                          <span className={`status-dot status-${d.status}`} />
                          {d.status}
                        </span>
                      </td>
                      <td>
                        {d.status === "queued" && (
                          <button
                            className="icon-btn visible"
                            title="Cancel"
                            onClick={() => cancelDocuments([d.id], { refreshPopup: true })}
                          >
                            ⛔
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <div className="confirm-actions">
              {queueDocs.some((d) => d.status === "queued") && (
                <button
                  className="btn-danger"
                  onClick={() =>
                    cancelDocuments(
                      queueDocs.filter((d) => d.status === "queued").map((d) => d.id),
                      { refreshPopup: true }
                    )
                  }
                >
                  Cancel all queued
                </button>
              )}
              <button className="btn-secondary" onClick={() => setShowQueuePopup(false)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {showErrorsPopup && (
        <div className="confirm-overlay" onClick={() => setShowErrorsPopup(false)}>
          <div className="dupe-modal" onClick={(e) => e.stopPropagation()}>
            <h2>Errors</h2>
            <p>Documents that failed to process. Re-scan retries OCR/extraction from the file already on disk.</p>

            {errorDocs.length === 0 && <p>No errors right now.</p>}

            {errorDocs.length > 0 && (
              <table className="popup-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Error</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {errorDocs.map((d) => (
                    <tr key={d.id}>
                      <td className="popup-name-cell" title={d.original_filename}>
                        {d.canonical_filename ?? d.original_filename}
                      </td>
                      <td className="popup-error-cell" title={d.error ?? undefined}>
                        {d.error ?? "—"}
                      </td>
                      <td>
                        <button
                          className="icon-btn visible"
                          title="Re-scan"
                          onClick={() => bulkReprocess([d.id], true)}
                        >
                          ♻️
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <div className="confirm-actions">
              {errorDocs.length > 0 && (
                <button
                  className="btn-primary"
                  onClick={() => bulkReprocess(errorDocs.map((d) => d.id), true)}
                >
                  ♻️ Re-scan all
                </button>
              )}
              <button className="btn-secondary" onClick={() => setShowErrorsPopup(false)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {confirm && (
        <div className="confirm-overlay" onClick={() => setConfirm(null)}>
          <div className="confirm-box" onClick={(e) => e.stopPropagation()}>
            <p>{confirm.message}</p>
            <div className="confirm-actions">
              <button className="btn-secondary" onClick={() => setConfirm(null)}>
                Cancel
              </button>
              <button
                className={confirm.danger === false ? "btn-primary" : "btn-danger"}
                onClick={() => {
                  confirm.onConfirm();
                  setConfirm(null);
                }}
              >
                {confirm.confirmLabel ?? "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
