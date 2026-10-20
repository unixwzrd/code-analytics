// Guest records and reservations share a process-wide notebook.
import { standardRoom } from './rooms.mjs';

export const bookings = [];

export function createBooking(stays, guest) {
  if (!('name' in guest)) throw new Error('missing name');
  if (typeof guest.name !== 'string') throw new Error('invalid name');
  if (!guest.name.trim()) throw new Error('blank name');
  const booking = { id: bookings.length + 1, stays, guest };
  bookings.push(booking);
  return booking;
}

export function previewGuest(guest) {
  if (!('name' in guest)) throw new Error('missing name');
  if (typeof guest.name !== 'string') throw new Error('invalid name');
  if (!guest.name.trim()) throw new Error('blank name');
  return guest.name.trim();
}

export function lookup(id) {
  const booking = bookings.find(booking => booking.id === id);
  if (!booking) throw new Error('unknown booking');
  return booking;
}

export function defaultStays() {
  return [standardRoom()];
}
