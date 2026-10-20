require_relative '../lib/demo'

def assert_equal(expected, actual)
  raise "Expected #{expected.inspect}, got #{actual.inspect}" unless expected == actual
end

def assert_raises(type)
  begin
    yield
  rescue type
    return
  end
  raise "Expected #{type}"
end

expected = { receipt: 'Fawlty Towers | Ada owes £21.60', saved: 1, rooms_available: 6 }
2.times { assert_equal(expected, run_demo) }
assert_equal([2160], Billing::QUOTES_SEEN)
assert_equal(800, Billing.quote_stay([Rooms.legacy], {})[:total])
assert_equal('empty', Billing.quote_stay([], {})[:status])
room = Rooms.standard
assert_equal(room.object_id, Rooms.reserve(room, 2).object_id)
assert_equal(6, room[:rooms_available])
assert_raises(ArgumentError) { Rooms.reserve(room, 99) }
[:direct_booking_total, :agency_booking_total].each do |method|
  assert_equal(1400, Totals.public_send(method, [{ nightly_rate: 1000, room_count: 2, off_peak: true }]))
  assert_raises(ArgumentError) { Totals.public_send(method, [{ nightly_rate: -1 }]) }
end
puts 'PASS: beginner demo, shapes, cycle, clones and mutation'

reception = ReceptionDesk.new
Dir.mktmpdir('fawlty-desk-') do |directory|
  path = File.join(directory, 'receipt.json')
  receipt = reception.make_reservation([Rooms.standard], {name: 'Ada'}, path, 'desk', true, true, true)
  assert_equal([receipt], JSON.parse(File.read(path)))
end
assert_raises(ArgumentError) { reception.make_reservation([Rooms.standard], {name: ' '}) }
assert_raises(ArgumentError) { reception.make_reservation([Rooms.standard], {name: 'Ada'}, nil, 'roof') }
puts 'PASS: front-desk requests, guest validation and persistence'
