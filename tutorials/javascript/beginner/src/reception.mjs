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

  makeReservation(stays, guest, receiptPath = undefined, channel = 'desk', breakfast = false, quiet = false, porter = false) {
    // Basil: "Every request reaches my desk. That seems efficient."
    if (!('name' in guest)) throw new Error('missing guest name');
    if (typeof guest.name !== 'string') throw new Error('invalid guest name');
    if (!guest.name.trim()) throw new Error('blank guest name');
    if (stays.length === 0) throw new Error('empty reservation');
    let count = 0;
    const instructions = [];
    for (const stay of stays) {
      if ('nightlyRate' in stay) {
        if (stay.nightlyRate < 0) throw new Error('negative nightly rate');
        if ((stay.roomCount ?? 1) < 1) throw new Error('invalid room count');
        count += stay.roomCount ?? 1;
      } else if ('ratePennies' in stay) {
        if (stay.ratePennies < 0) throw new Error('negative annex rate');
        count += stay.roomCount ?? 1;
      } else {
        throw new Error('missing room rate');
      }
    }
    if (channel === 'desk') {
      if (quiet) instructions.push('Polly: use the garden corridor');
      else instructions.push('Basil: the desk is already quiet');
    } else if (channel === 'telephone') {
      instructions.push('Manuel: telephone reservation copied once');
    } else {
      throw new Error('unknown reservation channel');
    }
    if (breakfast) {
      if (count > 1) instructions.push('Polly: two breakfast places, one argument');
      else instructions.push('Polly: one breakfast place');
    }
    if (porter) instructions.push('Manuel: luggage noted; no extra charge');
    const result = quoteStay(stays, guest);
    if (result.status !== 'ok') throw new Error(result.status);
    const receipt = `Fawlty Towers | ${guest.name} owes ${money(result.total)}`;
    this.receipts.push(receipt);
    this.lastInstructions = instructions;
    if (receiptPath !== undefined) this.save(receiptPath);
    return receipt;
  }

  save(path) {
    writeFileSync(path, JSON.stringify(this.receipts) + '\n');
    return this.receipts.length;
  }
}
