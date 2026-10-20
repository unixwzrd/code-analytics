// Polly keeps the rooms available; Basil keeps changing their field names.
import { money } from './billing.mjs';

export function standardRoom() {
  return { roomType: 'DOUBLE', nightlyRate: 1200, roomsAvailable: 8, wing: 'east' };
}

export function annexRoom() {
  return { code: 'ANNEX', ratePennies: 800, vacancies: 4, offPeak: true };
}

export function displayRate(room) {
  return money(room.nightlyRate ?? room.ratePennies);
}

export function reserve(room, roomCount) {
  if (roomCount <= 0) throw new Error('room count must be positive');
  const field = 'roomsAvailable' in room ? 'roomsAvailable' : 'vacancies';
  if (room[field] < roomCount) throw new Error('insufficient rooms');
  room[field] -= roomCount;
  return room;
}
