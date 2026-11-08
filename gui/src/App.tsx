import { useCallback, useEffect, useState } from "react";
import {
  MessageSquarePlus,
  Moon,
  Pencil,
  Plus,
  ShieldAlert,
  Sun,
} from "lucide-react";
import ChatPane from "./ChatPane";
import {
  api,
  type ChatInfo,
  type HistoryMessage,
  type StatusInfo,
} from "./api";

function useTheme() {
  const [dark, setDark] = useState(
    () =>
      localStorage.getItem("muse-linux-theme") === "dark" ||
      (!localStorage.getItem("muse-linux-theme") &&
        window.matchMedia("(prefers-color-scheme: dark)").matches),
  );
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("muse-linux-theme", dark ? "dark" : "light");
  }, [dark]);
  return { dark, toggle: () => setDark((d) => !d) };
}

export default function App() {
  const { dark, toggle } = useTheme();
  const [chats, setChats] = useState<ChatInfo[]>([]);
  const [selected, setSelected] = useState<string>("main");
  const [status, setStatus] = useState<StatusInfo | null>(null);
  const [history, setHistory] = useState<HistoryMessage[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [addId, setAddId] = useState("");
  const [addLabel, setAddLabel] = useState("");

  const refreshChats = useCallback(async () => {
    try {
      const data = await api.chats();
      setChats(data.chats);
    } catch {
      // backend unreachable; status line reports it
    }
  }, []);

  useEffect(() => {
    refreshChats();
    api
      .status()
      .then(setStatus)
      .catch(() => setStatus({ error: "backend unreachable" }));
  }, [refreshChats]);

  useEffect(() => {
    let live = true;
    setHistory(null);
    setHistoryError(null);
    setApprovals(null);
    api
      .history(selected)
      .then((data) => {
        if (live) setHistory(data.messages);
      })
      .catch((err) => {
        if (live) {
          setHistory([]);
          setHistoryError(String(err));
        }
      });
    return () => {
      live = false;
    };
  }, [selected]);

  const onWorkingStatus = useCallback(
    (code: string | null) => setWorking(code),
    [],
  );
  const onApprovals = useCallback(
    (data: Record<string, unknown> | null) => {
      // Only surface snapshots that actually carry content.
      if (data && Object.keys(data).length > 0) setApprovals(data);
    },
    [],
  );

  async function newChat() {
    const chat = await api.createChat("New chat");
    await refreshChats();
    setSelected(chat.id);
  }

  async function submitAdd() {
    if (!addId.trim()) return;
    const chat = await api.addChat(addId.trim(), addLabel.trim() || "Chat");
    setShowAdd(false);
    setAddId("");
    setAddLabel("");
    await refreshChats();
    setSelected(chat.id);
  }

  async function rename(chat: ChatInfo) {
    const label = window.prompt("Chat label", chat.label);
    if (!label || label === chat.label) return;
    await api.renameChat(chat.id, label);
    await refreshChats();
  }

  const connected = status && !status.error && status.paired;
  const selectedChat = chats.find((c) => c.id === selected);

  return (
    <div className="flex h-full bg-white text-zinc-900 dark:bg-zinc-950 dark:text-zinc-50">
      <aside className="flex w-72 shrink-0 flex-col border-r border-zinc-200 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex items-center justify-between px-4 pb-2 pt-4">
          <div>
            <h1 className="text-[17px] font-semibold tracking-tight">
              Muse for Linux
            </h1>
            <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
              Unofficial client
            </p>
          </div>
          <button
            onClick={toggle}
            className="rounded-full p-2 text-zinc-500 transition hover:bg-zinc-200 hover:text-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            aria-label="Toggle theme"
          >
            {dark ? <Sun size={17} /> : <Moon size={17} />}
          </button>
        </div>
        <div className="px-3 pb-2">
          <button
            onClick={newChat}
            className="flex w-full items-center gap-2 rounded-xl bg-indigo-600 px-3 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-indigo-500"
          >
            <MessageSquarePlus size={16} /> New chat
          </button>
          <button
            onClick={() => setShowAdd((s) => !s)}
            className="mt-1.5 flex w-full items-center gap-2 rounded-xl px-3 py-1.5 text-[13px] text-zinc-600 transition hover:bg-zinc-200 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            <Plus size={14} /> Add a chat by id
          </button>
          {showAdd && (
            <div className="mt-2 space-y-1.5 rounded-xl border border-zinc-200 bg-white p-2.5 dark:border-zinc-700 dark:bg-zinc-950">
              <input
                value={addId}
                onChange={(e) => setAddId(e.target.value)}
                placeholder="Chat (session) id"
                className="w-full rounded-lg border border-zinc-300 bg-transparent px-2 py-1.5 text-[13px] outline-none focus:border-indigo-500 dark:border-zinc-700"
              />
              <input
                value={addLabel}
                onChange={(e) => setAddLabel(e.target.value)}
                placeholder="Label"
                className="w-full rounded-lg border border-zinc-300 bg-transparent px-2 py-1.5 text-[13px] outline-none focus:border-indigo-500 dark:border-zinc-700"
              />
              <button
                onClick={submitAdd}
                className="w-full rounded-lg bg-zinc-800 py-1.5 text-[13px] font-medium text-white dark:bg-zinc-100 dark:text-zinc-900"
              >
                Add chat
              </button>
              <p className="text-[11px] leading-snug text-zinc-500">
                The server offers this client no chat list, so chats are
                tracked locally: ones you create here, plus any id you
                add.
              </p>
            </div>
          )}
        </div>
        <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 pb-3">
          {chats.map((chat) => (
            <div
              key={chat.id}
              className={`group flex items-center gap-1 rounded-xl px-3 py-2 text-sm transition ${
                chat.id === selected
                  ? "bg-zinc-200 font-medium dark:bg-zinc-800"
                  : "hover:bg-zinc-200/70 dark:hover:bg-zinc-800/70"
              }`}
            >
              <button
                className="flex-1 truncate text-left"
                onClick={() => setSelected(chat.id)}
              >
                {chat.label}
                {chat.is_main && (
                  <span className="ml-2 rounded-full bg-indigo-100 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">
                    MAIN
                  </span>
                )}
              </button>
              {!chat.is_main && (
                <button
                  onClick={() => rename(chat)}
                  className="rounded p-1 text-zinc-400 opacity-0 transition hover:text-zinc-700 group-hover:opacity-100 dark:hover:text-zinc-200"
                  aria-label="Rename"
                >
                  <Pencil size={13} />
                </button>
              )}
            </div>
          ))}
        </nav>
        <div className="border-t border-zinc-200 px-4 py-3 text-[12px] dark:border-zinc-800">
          <div className="flex items-center gap-2">
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                connected ? "bg-emerald-500" : "bg-red-500"
              }`}
            />
            {connected ? (
              <span>
                Paired host connected
                {status?.vm_count != null &&
                  ` · ${status.vm_count} VM${status.vm_count === 1 ? "" : "s"}`}
              </span>
            ) : (
              <span>Backend not reachable</span>
            )}
          </div>
          <p className="mt-1 text-zinc-500 dark:text-zinc-400">
            Via the Muse Gadget SDK pairing on this machine.
          </p>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-zinc-200 px-5 py-3 dark:border-zinc-800">
          <div className="min-w-0">
            <h2 className="truncate text-[15px] font-semibold">
              {selectedChat?.label ?? "Chat"}
            </h2>
            <p className="truncate text-[12px] text-zinc-500 dark:text-zinc-400">
              {selectedChat?.is_main ? "Your main Muse chat" : "Side chat"}
            </p>
          </div>
          {working && (
            <div className="flex items-center gap-2 rounded-full bg-amber-100 px-3 py-1 text-[12px] font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-200">
              <span className="relative flex h-2 w-2">
                <span className="absolute h-full w-full animate-ping rounded-full bg-amber-500 opacity-70" />
                <span className="h-2 w-2 rounded-full bg-amber-500" />
              </span>
              Muse is {working}
            </div>
          )}
        </header>

        {approvals && (
          <div className="mx-4 mt-3 flex gap-3 rounded-2xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-100">
            <ShieldAlert className="mt-0.5 shrink-0" size={18} />
            <div className="min-w-0">
              <p className="font-medium">Approval state reported</p>
              <p className="mt-0.5 break-words text-[13px] opacity-80">
                {JSON.stringify(approvals).slice(0, 400)}
              </p>
              <button
                className="mt-1 text-[12px] font-medium underline"
                onClick={() => setApprovals(null)}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        <div className="min-h-0 flex-1">
          {history === null ? (
            <div className="flex h-full items-center justify-center text-sm text-zinc-500">
              Loading history...
            </div>
          ) : (
            <>
              {historyError && (
                <p className="px-5 pt-3 text-[13px] text-red-600 dark:text-red-400">
                  History could not be loaded ({historyError}). Sending
                  still works.
                </p>
              )}
              <ChatPane
                key={selected}
                chatId={selected}
                initialMessages={history}
                onWorkingStatus={onWorkingStatus}
                onApprovals={onApprovals}
              />
            </>
          )}
        </div>
      </main>
    </div>
  );
}
