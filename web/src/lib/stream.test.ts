import { afterEach, expect, test, vi } from 'vitest';
import { transcriptStream } from './stream';

class FakeStream extends EventTarget {
  static CLOSED = 2;
  static instances: FakeStream[] = [];
  readyState = 0;
  onopen = () => {};
  onerror = () => {};
  constructor(readonly url: string) {
    super();
    FakeStream.instances.push(this);
  }
  close() {
    this.readyState = FakeStream.CLOSED;
  }
}
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  FakeStream.instances = [];
});

test('a permanently closed EventSource retries with backoff, reports rejection and cancels on unmount', async () => {
  vi.useFakeTimers();
  vi.stubGlobal('EventSource', FakeStream);
  const status = vi.fn();
  const opened = vi.fn();
  const transcript = vi.fn();
  const connection = transcriptStream({
    url: '/api/stream',
    status,
    opened,
    failed: vi.fn(),
    transcript,
    reconcile: vi.fn(),
  });
  const first = FakeStream.instances[0];
  if (!first) throw new Error('missing stream');
  first.readyState = FakeStream.CLOSED;
  first.onerror();
  expect(status).toHaveBeenLastCalledWith(
    'Stream unavailable or too many open streams; retrying',
  );
  await vi.advanceTimersByTimeAsync(999);
  expect(FakeStream.instances).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(1);
  const second = FakeStream.instances[1];
  if (!second) throw new Error('missing retry');
  second.readyState = FakeStream.CLOSED;
  second.onerror();
  await vi.advanceTimersByTimeAsync(1999);
  expect(FakeStream.instances).toHaveLength(2);
  await vi.advanceTimersByTimeAsync(1);
  const third = FakeStream.instances[2];
  if (!third) throw new Error('missing retry');
  third.onopen();
  expect(status).toHaveBeenLastCalledWith('Connected');
  expect(opened).toHaveBeenCalledOnce();
  third.dispatchEvent(new Event('transcript'));
  expect(transcript).toHaveBeenCalledOnce();
  third.readyState = FakeStream.CLOSED;
  third.onerror();
  connection.close();
  await vi.advanceTimersByTimeAsync(60000);
  expect(FakeStream.instances).toHaveLength(3);
});
