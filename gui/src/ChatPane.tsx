import {
  createContext,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiState,
  useLocalRuntime,
  type ChatModelAdapter,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import {
  Loader2,
  Mic,
  SendHorizontal,
  Square,
  Volume2,
} from "lucide-react";
import {
  fetchTts,
  streamMessage,
  streamVoice,
  type HistoryMessage,
  type PendingApproval,
} from "./api";
import { blobToWav16k } from "./wav";

interface ChatPaneProps {
  chatId: string;
  initialMessages: HistoryMessage[];
  onWorkingStatus: (code: string | null) => void;
  onApprovals: (pending: PendingApproval[] | null) => void;
  canLoadOlder?: boolean;
  onLoadOlder?: () => void;
}

/** Chat id plus the server message ids TTS can speak (history only). */
const ListenContext = createContext<{
  chatId: string;
  serverMessageIds: ReadonlySet<string>;
} | null>(null);

function toThreadMessages(messages: HistoryMessage[]): ThreadMessageLike[] {
  return messages.map((m, i) => ({
    id: m.message_id ?? `history-${i}`,
    role: m.role,
    content: [{ type: "text", text: m.text }],
    ...(m.occurred_at_ms
      ? { createdAt: new Date(m.occurred_at_ms) }
      : {}),
  }));
}

function lastUserText(messages: readonly { content: readonly unknown[] }[]) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const parts = messages[i].content as { type: string; text?: string }[];
    const text = parts
      .filter((p) => p.type === "text")
      .map((p) => p.text ?? "")
      .join("");
    if (text) return text;
  }
  return "";
}

function TextPart({ text }: { text: string }) {
  return <span className="whitespace-pre-wrap break-words">{text}</span>;
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="flex justify-end px-4 py-1.5">
      <div className="max-w-[80%] rounded-2xl rounded-br-md bg-indigo-600 px-4 py-2.5 text-[15px] leading-relaxed text-white shadow-sm">
        <MessagePrimitive.Parts components={{ Text: TextPart }} />
      </div>
    </MessagePrimitive.Root>
  );
}

function ListenButton() {
  const messageId = useAuiState((s) => s.message.id);
  const listen = useContext(ListenContext);
  const [phase, setPhase] = useState<"idle" | "loading" | "playing">(
    "idle",
  );
  if (!listen || !listen.serverMessageIds.has(messageId)) return null;

  async function play() {
    if (!listen || phase !== "idle") return;
    setPhase("loading");
    try {
      const blob = await fetchTts(listen.chatId, messageId);
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      const settle = () => {
        setPhase("idle");
        URL.revokeObjectURL(url);
      };
      audio.onended = settle;
      audio.onerror = settle;
      setPhase("playing");
      await audio.play();
    } catch {
      setPhase("idle");
    }
  }

  return (
    <button
      onClick={play}
      disabled={phase !== "idle"}
      aria-label="Listen to this reply"
      title="Listen to this reply"
      className="mt-1.5 flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[12px] text-zinc-500 transition hover:bg-zinc-200 hover:text-zinc-800 disabled:opacity-50 dark:text-zinc-400 dark:hover:bg-zinc-700 dark:hover:text-zinc-100"
    >
      {phase === "idle" ? (
        <Volume2 size={14} />
      ) : (
        <Loader2 size={14} className="animate-spin" />
      )}
      {phase === "playing" ? "Playing..." : "Listen"}
    </button>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="flex justify-start px-4 py-1.5">
      <div className="max-w-[85%] rounded-2xl rounded-bl-md bg-zinc-100 px-4 py-2.5 text-[15px] leading-relaxed text-zinc-900 shadow-sm dark:bg-zinc-800 dark:text-zinc-50">
        <MessagePrimitive.Parts components={{ Text: TextPart }} />
        <ListenButton />
      </div>
    </MessagePrimitive.Root>
  );
}

export default function ChatPane({
  chatId,
  initialMessages,
  onWorkingStatus,
  onApprovals,
  canLoadOlder,
  onLoadOlder,
}: ChatPaneProps) {
  // WAV bytes waiting for the next adapter run to send as a voice turn.
  const pendingVoice = useRef<ArrayBuffer | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const [recording, setRecording] = useState(false);
  const [voiceError, setVoiceError] = useState<string | null>(null);

  const serverMessageIds = useMemo(
    () =>
      new Set(
        initialMessages
          .map((m) => m.message_id)
          .filter((id): id is string => Boolean(id)),
      ),
    [initialMessages],
  );

  const adapter = useMemo<ChatModelAdapter>(
    () => ({
      async *run({ messages, abortSignal }) {
        const voice = pendingVoice.current;
        pendingVoice.current = null;
        const text = lastUserText(messages as never);
        if (!text && !voice) return;
        onWorkingStatus("starting");
        let snapshot = "";
        try {
          const stream = voice
            ? streamVoice(chatId, voice, abortSignal)
            : streamMessage(chatId, text, abortSignal);
          for await (const event of stream) {
            if (event.type === "status") {
              onWorkingStatus(event.code);
            } else if (event.type === "approvals") {
              onApprovals(event.pending);
            } else if (event.type === "reply") {
              snapshot = event.text;
              yield {
                content: [{ type: "text", text: snapshot }],
                status: { type: "running" },
              };
            } else if (event.type === "error") {
              throw new Error(event.error);
            }
          }
        } finally {
          onWorkingStatus(null);
        }
        yield {
          content: [{ type: "text", text: snapshot }],
          status: { type: "complete", reason: "stop" },
        };
      },
    }),
    [chatId, onWorkingStatus, onApprovals],
  );

  const runtime = useLocalRuntime(adapter, {
    initialMessages: toThreadMessages(initialMessages),
  });

  async function toggleRecording() {
    if (recording) {
      recorderRef.current?.stop();
      return;
    }
    setVoiceError(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setVoiceError(
        "Microphone access was denied, so no voice message was recorded.",
      );
      return;
    }
    const recorder = new MediaRecorder(stream);
    const chunks: Blob[] = [];
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.push(event.data);
    };
    recorder.onstop = async () => {
      stream.getTracks().forEach((track) => track.stop());
      setRecording(false);
      try {
        const wav = await blobToWav16k(
          new Blob(chunks, { type: recorder.mimeType || "audio/webm" }),
        );
        pendingVoice.current = wav;
        runtime.thread.append("🎙️ Voice message");
      } catch {
        setVoiceError("That recording could not be converted to send.");
      }
    };
    recorderRef.current = recorder;
    recorder.start();
    setRecording(true);
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ListenContext.Provider value={{ chatId, serverMessageIds }}>
        <ThreadPrimitive.Root className="flex h-full flex-col bg-white dark:bg-zinc-950">
          <ThreadPrimitive.Viewport className="flex flex-1 flex-col overflow-y-auto scroll-smooth py-4">
            {canLoadOlder && onLoadOlder && (
              <div className="flex justify-center pb-3">
                <button
                  onClick={onLoadOlder}
                  className="rounded-full border border-zinc-300 px-3.5 py-1 text-[12.5px] font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                >
                  Load older messages
                </button>
              </div>
            )}
            <ThreadPrimitive.Empty>
              <div className="flex flex-1 items-center justify-center px-6 text-center">
                <div>
                  <p className="text-lg font-medium text-zinc-800 dark:text-zinc-100">
                    Start the conversation
                  </p>
                  <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
                    Messages go to your Muse on the paired Linux host, in
                    this chat, with its history intact.
                  </p>
                </div>
              </div>
            </ThreadPrimitive.Empty>
            <ThreadPrimitive.Messages
              components={{ UserMessage, AssistantMessage }}
            />
          </ThreadPrimitive.Viewport>
          <div className="border-t border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-950">
            <ComposerPrimitive.Root className="mx-auto flex max-w-3xl items-end gap-2 rounded-2xl border border-zinc-300 bg-zinc-50 px-3 py-2 shadow-sm focus-within:border-indigo-500 dark:border-zinc-700 dark:bg-zinc-900">
              <ComposerPrimitive.Input
                placeholder="Message Muse..."
                className="max-h-40 flex-1 resize-none bg-transparent px-1 py-1.5 text-[15px] text-zinc-900 outline-none placeholder:text-zinc-400 dark:text-zinc-50"
                rows={1}
                autoFocus
              />
              <button
                onClick={toggleRecording}
                aria-label={
                  recording ? "Stop recording" : "Record a voice message"
                }
                title={
                  recording ? "Stop recording" : "Record a voice message"
                }
                className={`rounded-full p-2 text-white transition ${
                  recording
                    ? "animate-pulse bg-red-600 hover:bg-red-500"
                    : "bg-zinc-500 hover:bg-zinc-400"
                }`}
              >
                {recording ? <Square size={15} /> : <Mic size={16} />}
              </button>
              <ThreadPrimitive.If running={false}>
                <ComposerPrimitive.Send asChild>
                  <button
                    className="rounded-full bg-indigo-600 p-2 text-white transition hover:bg-indigo-500 disabled:opacity-40"
                    aria-label="Send"
                  >
                    <SendHorizontal size={17} />
                  </button>
                </ComposerPrimitive.Send>
              </ThreadPrimitive.If>
              <ThreadPrimitive.If running>
                <ComposerPrimitive.Cancel asChild>
                  <button
                    className="rounded-full bg-zinc-700 p-2 text-white transition hover:bg-zinc-600"
                    aria-label="Stop"
                  >
                    <Square size={15} />
                  </button>
                </ComposerPrimitive.Cancel>
              </ThreadPrimitive.If>
            </ComposerPrimitive.Root>
            {voiceError && (
              <p className="mx-auto mt-2 max-w-3xl text-center text-[12px] text-red-600 dark:text-red-400">
                {voiceError}
              </p>
            )}
            <p className="mx-auto mt-2 max-w-3xl text-center text-[11px] text-zinc-400 dark:text-zinc-500">
              Muse for Linux (unofficial). Replies stream from the Muse
              service through the paired host.
            </p>
          </div>
        </ThreadPrimitive.Root>
      </ListenContext.Provider>
    </AssistantRuntimeProvider>
  );
}
