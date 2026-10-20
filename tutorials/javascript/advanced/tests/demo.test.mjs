import test from 'node:test';
import assert from 'node:assert/strict';
import { runDemo } from '../src/demo.mjs';
import { standardRoom, annexRoom, reserve } from '../src/rooms.mjs';
import { quoteStay } from '../src/billing.mjs';
import { directBookingTotal, agencyBookingTotal } from '../src/reception.mjs';

test('repeatable demo, real temporary persistence and safe ESM cycle', () => {
  const expected = { receipt: 'Fawlty Towers | Ada owes £21.60', saved: 1, rooms_available: 6, total: 2160, batch_total: 2160 };
  assert.deepEqual(runDemo(), expected);
  assert.deepEqual(runDemo(), expected);
});

test('billing branches and two object shapes', () => {
  const item = { ...standardRoom(), roomCount: 3 };
  assert.equal(quoteStay([item], { tier: 'gold' }, 'SAVE10', 'post').total, 2930);
  assert.equal(quoteStay([annexRoom()], {}).total, 560);
  assert.equal(quoteStay([], {}).status, 'empty');
  assert.equal(quoteStay([item], { blocked: true }).status, 'blocked');
  assert.equal(quoteStay([item], {}, '', 'moon').status, 'unknown statement');
});

test('clones agree and reject negative prices', () => {
  const rows = [{ nightlyRate: 500, roomCount: 2 }, { nightlyRate: 1000, offPeak: true }];
  for (const total of [directBookingTotal, agencyBookingTotal]) {
    assert.equal(total(rows), 1700);
    assert.throws(() => total([{ nightlyRate: -1 }]));
  }
});

test('reserve mutates the input and rejects insufficient rooms', () => {
  const room = standardRoom();
  assert.equal(reserve(room, 2), room);
  assert.equal(room.roomsAvailable, 6);
  assert.throws(() => reserve(room, 99));
});

import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { HotelOffice, runBatchDemo, pending } from '../src/hotel_queue.mjs';

test('queue states, cache, aliased jobs and JSON persistence', () => {
  const first = runBatchDemo();
  assert.equal(first.total, 2160);
  assert.deepEqual(first.events, ['bill:A', 'cancel:B']);
  const jobs = [
    { id: 'A' },
    { id: 'C', guest: { blocked: true } },
    { id: 'D', guest: { name: 'Ada' }, attempts: 3 },
    { id: 'E' },
    { id: 'F', guest: { name: 'Ada' }, stays: [] },
  ];
  const office = new HotelOffice();
  office.load(jobs);
  const result = office.processQueue();
  assert.equal(result.total, 2160);
  assert.deepEqual(jobs.map(job => job.state), [undefined, 'held', 'exhausted', 'invalid', 'invalid']);
  assert.deepEqual(result.results[0], first.results[0]);
  assert.deepEqual(pending, []);
  const directory = mkdtempSync(join(tmpdir(), 'fawlty-queue-test-'));
  try {
    const path = join(directory, 'batch.json');
    office.export(path, result);
    assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')), result);
  } finally {
    rmSync(directory, { recursive: true });
  }
});
