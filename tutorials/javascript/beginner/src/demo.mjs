import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { standardRoom, reserve } from './rooms.mjs';
import { quotesSeen } from './billing.mjs';
import { ReceptionDesk } from './reception.mjs';

export function runDemo() {
  quotesSeen.length = 0;
  const room = standardRoom();
  reserve(room, 2);
  const desk = new ReceptionDesk();
  const receipt = desk.makeReservation([{ ...room, roomCount: 2 }], { name: 'Ada', tier: 'gold' });
  const directory = mkdtempSync(join(tmpdir(), 'fawlty-reservation-'));
  try {
    const saved = desk.save(join(directory, 'receipts.json'));
    return { receipt, saved, rooms_available: room.roomsAvailable };
  } finally {
    rmSync(directory, { recursive: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  console.log(JSON.stringify(runDemo()));
}
