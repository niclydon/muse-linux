import { useMemo } from "react";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useLocalRuntime,
  type ChatModelAdapter,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { SendHorizontal, Square } from "lucide-react";
import { streamMessage, type HistoryMessage } from "./api";

interface ChatPaneProps {
  chatId: string;
  initialMessages: HistoryMessage[];
  onWorkingStatus: (code: string | null) => void;
  onApprovals: (data: Record<string, unknown> | null) => void;
}

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

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="flex justify-start px-4 py-1.5">
      <div className="max-w-[85%] rounded-2xl rounded-bl-md bg-zinc-100 px-4 py-2.5 text-[15px] leading-relaxed text-zinc-900 shadow-sm dark:bg-zinc-800 dark:text-zinc-50">
        <MessagePrimitive.Parts components={{ Text: TextPart }} />
      </div>
    </MessagePrimitive.Root>
  );
}

export default function ChatPane({
  chatId,
  initialMessages,
  onWorkingStatus,
  onApprovals,
}: ChatPaneProps) {
  const adapter = useMemo<ChatModelAdapter>(
    () => ({
      async *run({ messages, abortSignal }) {
        const text = lastUserText(messages as never);
        if (!text) return;
        onWorkingStatus("starting");
        let snapshot = "";
        try {
          for await (const event of streamMessage(chatId, text, abortSignal)) {
            if (event.type === "status") {
              onWorkingStatus(event.code);
            } else if (event.type === "approvals") {
              onApprovals(event.data);
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

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="flex h-full flex-col bg-white dark:bg-zinc-950">
        <ThreadPrimitive.Viewport className="flex flex-1 flex-col overflow-y-auto scroll-smooth py-4">
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
          <p className="mx-auto mt-2 max-w-3xl text-center text-[11px] text-zinc-400 dark:text-zinc-500">
            Muse for Linux (unofficial). Replies stream from the Muse
            service through the paired host.
          </p>
        </div>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}
