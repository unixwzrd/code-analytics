// Sybil asked for a queue. Basil supplied a queue, a cache and a filing dispute.
import { writeFileSync } from 'node:fs';
import { standardRoom } from './rooms.mjs';
import { createBooking } from './bookings.mjs';
import { quoteStay, money } from './billing.mjs';

export const pending = [];
export const events = [];
export const receiptCache = {};

export class HotelOffice {
  load(jobs) {
    pending.push(...jobs);
  }

  processQueue(statement = 'email') {
    const results = [];
    let total = 0;
    while (pending.length) {
      const job = pending.shift();
      if (job.cancelled) {
        job.state = 'cancelled';
        results.push({ id: job.id, reason: 'cancelled' });
        events.push('cancel:' + job.id);
      } else {
        if (job.id in receiptCache) {
          results.push(receiptCache[job.id]);
          total += receiptCache[job.id].total;
          events.push('cache:' + job.id);
        } else {
          if ('guest' in job) {
            if (job.guest.blocked) {
              job.state = 'held';
              results.push({ id: job.id, reason: 'blocked' });
            } else {
              if ((job.attempts ?? 0) < 3) {
                job.attempts = (job.attempts ?? 0) + 1;
                const quote = quoteStay(job.stays, job.guest, job.coupon ?? '', statement);
                if (quote.status === 'ok') {
                  const booking = createBooking(job.stays, job.guest);
                  const receipt = 'Fawlty Towers | ' + booking.guest.name + ' owes ' + money(quote.total);
                  const result = { id: job.id, receipt, total: quote.total, state: 'billed' };
                  receiptCache[job.id] = result;
                  results.push(result);
                  total += quote.total;
                  job.state = 'billed';
                  events.push('bill:' + job.id);
                } else {
                  job.state = 'invalid';
                  results.push({ id: job.id, reason: quote.status });
                }
              } else {
                job.state = 'exhausted';
                results.push({ id: job.id, reason: 'retry limit' });
              }
            }
          } else {
            job.state = 'invalid';
            results.push({ id: job.id, reason: 'missing guest' });
          }
        }
      }
    }
    return { total, results, events: [...events] };
  }

  export(path, result) {
    writeFileSync(path, JSON.stringify(result) + '\n');
  }
}

export function runBatchDemo() {
  pending.length = 0;
  events.length = 0;
  for (const key of Object.keys(receiptCache)) delete receiptCache[key];
  const office = new HotelOffice();
  office.load([
    { id: 'A', stays: [{ ...standardRoom(), roomCount: 2 }], guest: { name: 'Ada', tier: 'gold' } },
    { id: 'B', cancelled: true },
  ]);
  return office.processQueue();
}
