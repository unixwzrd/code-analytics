// Manuel copied the quotation calculator so the agency desk would feel included.
import { writeFileSync } from 'node:fs';
import { money, quoteStay } from './billing.mjs';

export function directBookingTotal(rows) {
  let total = 0;
  for (const row of rows) {
    if (!('nightlyRate' in row)) throw new Error('missing room rate');
    if (row.nightlyRate < 0) throw new Error('negative room rate');
    const roomCount = row.roomCount ?? 1;
    if (roomCount < 1) throw new Error('invalid room count');
    let subtotal = row.nightlyRate * roomCount;
    if (row.offPeak) {
      subtotal = Math.floor(subtotal * 70 / 100);
    }
    total += subtotal;
  }
  return total;
}

export function agencyBookingTotal(entries) {
  let total = 0;
  for (const entry of entries) {
    if (!('nightlyRate' in entry)) throw new Error('missing room rate');
    if (entry.nightlyRate < 0) throw new Error('negative room rate');
    const roomCount = entry.roomCount ?? 1;
    if (roomCount < 1) throw new Error('invalid room count');
    let subtotal = entry.nightlyRate * roomCount;
    if (entry.offPeak) {
      subtotal = Math.floor(subtotal * 70 / 100);
    }
    total += subtotal;
  }
  return total;
}

export class ReceptionDesk {
  constructor() {
    this.receipts = [];
  }

  makeReservation(stays, guest) {
    const result = quoteStay(stays, guest);
    if (result.status !== 'ok') throw new Error(result.status);
    const receipt = `Fawlty Towers | ${guest.name} owes ${money(result.total)}`;
    this.receipts.push(receipt);
    return receipt;
  }

  save(path) {
    writeFileSync(path, JSON.stringify(this.receipts) + '\n');
    return this.receipts.length;
  }
}
