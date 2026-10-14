export function transcriptStream({
  url,
  status,
  opened,
  failed,
  transcript,
  reconcile,
}: {
  url: string;
  status: (message: string) => void;
  opened: () => void;
  failed: () => void;
  transcript: (event: MessageEvent<string>) => void;
  reconcile: (event: MessageEvent<string>) => void;
}) {
  let stream: EventSource;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let attempts = 0;
  let stopped = false;
  const connect = () => {
    if (stopped) return;
    stream = new EventSource(url);
    stream.onopen = () => {
      attempts = 0;
      status('Connected');
      opened();
    };
    stream.onerror = () => {
      failed();
      if (stream.readyState === EventSource.CLOSED) {
        stream.close();
        status('Stream unavailable or too many open streams; retrying');
        clearTimeout(timer);
        timer = setTimeout(
          connect,
          Math.min(30000, 1000 * 2 ** Math.min(attempts++, 5)),
        );
      } else status('Reconnecting');
    };
    stream.addEventListener('transcript', transcript);
    stream.addEventListener('reconcile', reconcile);
  };
  connect();
  return {
    close() {
      stopped = true;
      clearTimeout(timer);
      stream.close();
    },
  };
}
