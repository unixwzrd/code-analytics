import test from 'node:test';
import assert from 'node:assert/strict';
import { runDemo } from '../src/demo.mjs';
import { standardRoom, annexRoom, reserve } from '../src/rooms.mjs';
import { quotesSeen, quoteStay } from '../src/billing.mjs';
import { directBookingTotal, agencyBookingTotal } from '../src/reception.mjs';

test('repeatable demo and safe import cycle', () => {
  const expected = { receipt: 'Fawlty Towers | Ada owes £21.60', saved: 1, rooms_available: 6 };
  assert.deepEqual(runDemo(), expected);
  assert.deepEqual(runDemo(), expected);
  assert.deepEqual(quotesSeen, [2160]);
});

test('two shapes and input mutation', () => {
  assert.equal(quoteStay([annexRoom()], {}).total, 800);
  assert.equal(quoteStay([], {}).status, 'empty');
  const room = standardRoom();
  assert.equal(reserve(room, 2), room);
  assert.equal(room.roomsAvailable, 6);
  assert.throws(() => reserve(room, 99));
});

test('duplicate totals agree', () => {
  for (const total of [directBookingTotal, agencyBookingTotal]) {
    assert.equal(total([{ nightlyRate: 1000, roomCount: 2, offPeak: true }]), 1400);
    assert.throws(() => total([{ nightlyRate: -1 }]));
  }
});

import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ReceptionDesk } from '../src/reception.mjs';

test('desk requests, guest validation and optional persistence', () => {
  const desk = new ReceptionDesk();
  const directory = mkdtempSync(join(tmpdir(), 'fawlty-desk-'));
  try {
    const path = join(directory, 'receipt.json');
    const receipt = desk.makeReservation([standardRoom()], {name: 'Ada'}, path, 'desk', true, true, true);
    assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')), [receipt]);
    assert.equal(desk.lastInstructions.length, 3);
  } finally {
    rmSync(directory, {recursive: true});
  }
  assert.throws(() => desk.makeReservation([standardRoom()], {name: ' '}));
  assert.throws(() => desk.makeReservation([standardRoom()], {name: 'Ada'}, undefined, 'roof'));
});
