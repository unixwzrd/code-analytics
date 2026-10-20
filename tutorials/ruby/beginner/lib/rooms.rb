require_relative 'billing'

module Rooms
  def self.standard
    { room_type: 'DOUBLE', nightly_rate: 1200, rooms_available: 8, wing: 'east' }
  end

  def self.legacy
    { room_code: 'ANNEX', rate_pennies: 800, vacancies: 4, off_peak: true }
  end

  def self.display_rate(room)
    Billing.money(room.fetch(:nightly_rate, room[:rate_pennies]))
  end

  def self.reserve(room, room_count)
    raise ArgumentError, 'room count must be positive' if room_count <= 0
    field = room.key?(:rooms_available) ? :rooms_available : :vacancies
    raise ArgumentError, 'insufficient rooms' if room[field] < room_count
    room[field] -= room_count
    room
  end
end
