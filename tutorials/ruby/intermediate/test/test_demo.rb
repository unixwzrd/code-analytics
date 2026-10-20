# Plain standard-library assertions: no minitest gem required.
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

expected = { receipt: 'Fawlty Towers | Ada owes £21.60', saved: 1, rooms_available: 6, total: 2160 }
2.times { assert_equal(expected, run_demo) }
item = Rooms.standard.merge(room_count: 3)
assert_equal(2930, Billing.quote_stay([item], { tier: 'gold' }, 'SAVE10', 'post')[:total])
assert_equal(560, Billing.quote_stay([Rooms.legacy], {})[:total])
assert_equal('empty', Billing.quote_stay([], {})[:status])
assert_equal('blocked', Billing.quote_stay([item], { blocked: true })[:status])
assert_equal('unknown statement', Billing.quote_stay([item], {}, '', 'moon')[:status])
rows = [{ nightly_rate: 500, room_count: 2 }, { nightly_rate: 1000, off_peak: true }]
[:direct_booking_total, :agency_booking_total].each do |method|
  assert_equal(1700, Totals.public_send(method, rows))
  assert_raises(ArgumentError) { Totals.public_send(method, [{ nightly_rate: -1 }]) }
end
room = Rooms.standard
assert_equal(room.object_id, Rooms.reserve(room, 2).object_id)
assert_equal(6, room[:rooms_available])
assert_raises(ArgumentError) { Rooms.reserve(room, 99) }
reception = ReceptionDesk.new
reception.make_reservation([Rooms.standard], { name: 'Ada' })
Dir.mktmpdir('fawlty-reservation-test-') do |directory|
  path = File.join(directory, 'receipts.json')
  assert_equal(1, reception.save(path))
  assert_equal(["Fawlty Towers | Ada owes £12.00"], JSON.parse(File.read(path)))
end
puts 'PASS: demo, pricing branches, clones, mutation, validation and persistence'
