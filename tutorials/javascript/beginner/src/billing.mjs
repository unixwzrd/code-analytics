import { ReceptionDesk } from './reception.mjs';

export const quotesSeen = [];

export function money(pennies) {
  return `£${Math.floor(pennies / 100)}.${String(pennies % 100).padStart(2, '0')}`;
}

export function quoteStay(stays, guest) {
  if (stays.length === 0) return { status: 'empty', total: 0 };
  let total = 0;
  for (const item of stays) {
    const nightlyRate = item.nightlyRate ?? item.ratePennies ?? 0;
    if (nightlyRate < 0) return { status: 'invalid room rate', total: 0 };
    let amount = nightlyRate * (item.roomCount ?? 1);
    if (guest.tier === 'gold') amount = Math.floor(amount * 90 / 100);
    total += amount;
  }
  quotesSeen.push(total);
  return { status: 'ok', total };
}

export function previewReceipt(stays, guest) {
  return new ReceptionDesk().makeReservation(stays, guest);
}
