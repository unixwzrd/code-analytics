# Guest records and reservations share a process-wide notebook.
require_relative 'rooms'

module Bookings
  RECORDS = []

  def self.create(stays, guest)
    raise ArgumentError, 'missing name' unless guest.key?(:name)
    raise ArgumentError, 'invalid name' unless guest[:name].is_a?(String)
    raise ArgumentError, 'blank name' if guest[:name].strip.empty?
    record = { id: RECORDS.length + 1, stays: stays, guest: guest }
    RECORDS << record
    record
  end

  def self.preview_guest(guest)
    raise ArgumentError, 'missing name' unless guest.key?(:name)
    raise ArgumentError, 'invalid name' unless guest[:name].is_a?(String)
    raise ArgumentError, 'blank name' if guest[:name].strip.empty?
    guest[:name].strip
  end

  def self.lookup(id)
    RECORDS.find { |record| record[:id] == id } || raise(KeyError, id)
  end

  def self.default_stays
    [Rooms.standard]
  end
end
