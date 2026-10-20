import test from 'node:test';
import assert from 'node:assert/strict';
import { runDemo } from '../src/demo.mjs';
import { standardRoom, annexRoom, reserve } from '../src/rooms.mjs';
import { quoteStay } from '../src/billing.mjs';
import { directBookingTotal, agencyBookingTotal } from '../src/reception.mjs';

test('repeatable demo, real temporary persistence and safe ESM cycle', () => {
  const expected = { receipt: 'Fawlty Towers | Ada owes £21.60', saved: 1, rooms_available: 6, total: 2160 };
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
