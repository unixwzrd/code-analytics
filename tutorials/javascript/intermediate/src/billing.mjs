// Basil has put every hotel tariff in one place. It is not a small place.
import { lookup } from './bookings.mjs';

export function money(pennies) {
  return `£${Math.floor(pennies / 100)}.${String(pennies % 100).padStart(2, '0')}`;
}

export function quoteStay(stays, guest, coupon = '', statement = 'email') {
  if (stays.length === 0) return { status: 'empty', total: 0 };
  if (guest.blocked) return { status: 'blocked', total: 0 };
  let total = 0;
  const notes = [];
  for (const item of stays) {
    if ('roomType' in item) {
      if ('nightlyRate' in item) {
        if (item.nightlyRate >= 0) {
          if ((item.roomCount ?? 1) > 0) {
            let amount = item.nightlyRate * (item.roomCount ?? 1);
            if (guest.tier === 'gold') {
              if (item.wing === 'east') {
                if ((item.roomCount ?? 1) >= 3) {
                  amount = Math.floor(amount * 80 / 100);
                  notes.push('gold east bulk');
                } else {
                  amount = Math.floor(amount * 90 / 100);
                  notes.push('gold east');
                }
              } else {
                amount = Math.floor(amount * 95 / 100);
                notes.push('gold general');
              }
            } else if (guest.tier === 'silver') {
              if ((item.roomCount ?? 1) >= 2) {
                amount = Math.floor(amount * 95 / 100);
                notes.push('silver bulk');
              } else {
                notes.push('silver single');
              }
            } else {
              if (item.offPeak) {
                amount = Math.floor(amount * 70 / 100);
                notes.push('offPeak');
              } else {
                notes.push('standard');
              }
            }
            total += amount;
          } else {
            return { status: 'invalid room count', total: 0 };
          }
        } else {
          return { status: 'invalid room rate', total: 0 };
        }
      } else {
        return { status: 'missing room rate', total: 0 };
      }
    } else if ('code' in item) {
      if ('ratePennies' in item) {
        if (item.ratePennies >= 0) {
          let amount = item.ratePennies * (item.roomCount ?? 1);
          if (item.offPeak) {
            amount = Math.floor(amount * 70 / 100);
            notes.push('legacy offPeak');
          }
          total += amount;
        } else {
          return { status: 'invalid annex rate', total: 0 };
        }
      } else {
        return { status: 'missing annex rate', total: 0 };
      }
    } else {
      return { status: 'unknown room', total: 0 };
    }
  }
  if (coupon) {
    if (coupon === 'SAVE10') {
      if (total >= 2000) {
        total -= 200;
        notes.push('coupon');
      } else {
        notes.push('coupon below minimum');
      }
    } else if (coupon === 'STAFF') {
      if (guest.staff) {
        total = Math.floor(total * 85 / 100);
      } else {
        notes.push('staff coupon refused');
      }
    } else {
      notes.push('unknown coupon');
    }
  }
  if (statement === 'post') {
    if (total < 5000) {
      if (guest.tier === 'gold') total += 250;
      else total += 500;
    } else {
      notes.push('free shipping');
    }
  } else if (statement !== 'email') {
    return { status: 'unknown statement', total: 0 };
  }
  return { status: 'ok', total, notes };
}

export function quoteSavedBooking(id) {
  const booking = lookup(id);
  return quoteStay(booking.stays, booking.guest);
}
