import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { standardRoom, reserve } from './rooms.mjs';
import { bookings, createBooking } from './bookings.mjs';
import { quoteSavedBooking } from './billing.mjs';
import { runBatchDemo } from './hotel_queue.mjs';
import { ReceptionDesk } from './reception.mjs';

// Legitimate orchestration; calls alone do not justify extraction.
export function runDemo() {
  bookings.length = 0;
  const room = standardRoom();
  reserve(room, 2);
  const guest = { name: 'Ada', tier: 'gold' };
  const booking = createBooking([{ ...room, roomCount: 2 }], guest);
  const desk = new ReceptionDesk();
  const receipt = desk.makeReservation(booking.stays, guest);
  const directory = mkdtempSync(join(tmpdir(), 'fawlty-reservation-'));
  try {
    const saved = desk.save(join(directory, 'receipts.json'));
    return { receipt, saved, rooms_available: room.roomsAvailable, total: quoteSavedBooking(booking.id).total, batch_total: runBatchDemo().total };
  } finally {
    rmSync(directory, { recursive: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  console.log(JSON.stringify(runDemo()));
}
