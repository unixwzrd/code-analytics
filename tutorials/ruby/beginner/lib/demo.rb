require 'json'
require 'tmpdir'
require_relative 'rooms'
require_relative 'reception'

def run_demo
  Billing::QUOTES_SEEN.clear
  room = Rooms.standard
  Rooms.reserve(room, 2)
  reception = ReceptionDesk.new
  receipt = reception.make_reservation([room.merge(room_count: 2)], { name: 'Ada', tier: 'gold' })
  saved = Dir.mktmpdir('fawlty-reservation-') do |directory|
    reception.save(File.join(directory, 'receipts.json'))
  end
  { receipt: receipt, saved: saved, rooms_available: room[:rooms_available] }
end

puts JSON.generate(run_demo) if $PROGRAM_NAME == __FILE__
