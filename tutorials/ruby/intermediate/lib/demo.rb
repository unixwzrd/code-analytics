require 'json'
require 'tmpdir'
require_relative 'rooms'
require_relative 'bookings'
require_relative 'reception'

# Legitimate orchestration; the helper coordinates a single use case.
def run_demo
  Bookings::RECORDS.clear
  room = Rooms.standard
  Rooms.reserve(room, 2)
  guest = { name: 'Ada', tier: 'gold' }
  booking = Bookings.create([room.merge(room_count: 2)], guest)
  reception = ReceptionDesk.new
  receipt = reception.make_reservation(booking[:stays], guest)
  saved = Dir.mktmpdir('fawlty-reservation-') do |directory|
    reception.save(File.join(directory, 'receipts.json'))
  end
  { receipt: receipt, saved: saved, rooms_available: room[:rooms_available],
    total: Billing.quote_saved_booking(booking[:id])[:total] }
end

puts JSON.generate(run_demo) if $PROGRAM_NAME == __FILE__
