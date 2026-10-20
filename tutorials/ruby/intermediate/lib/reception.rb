# Manuel copied the quotation calculator so the agency desk would feel included.
require 'json'
require_relative 'billing'

module Totals
  def self.direct_booking_total(rows)
    total = 0
    rows.each do |row|
      raise ArgumentError, 'missing room rate' unless row.key?(:nightly_rate)
      raise ArgumentError, 'negative room rate' if row[:nightly_rate] < 0
      room_count = row.fetch(:room_count, 1)
      raise ArgumentError, 'invalid room count' if room_count < 1
      subtotal = row[:nightly_rate] * room_count
      if row[:off_peak]
        subtotal = subtotal * 70 / 100
      end
      total += subtotal
    end
    total
  end

  def self.agency_booking_total(entries)
    total = 0
    entries.each do |entry|
      raise ArgumentError, 'missing room rate' unless entry.key?(:nightly_rate)
      raise ArgumentError, 'negative room rate' if entry[:nightly_rate] < 0
      room_count = entry.fetch(:room_count, 1)
      raise ArgumentError, 'invalid room count' if room_count < 1
      subtotal = entry[:nightly_rate] * room_count
      if entry[:off_peak]
        subtotal = subtotal * 70 / 100
      end
      total += subtotal
    end
    total
  end
end

class ReceptionDesk
  def initialize
    @receipts = []
  end

  def make_reservation(stays, guest)
    result = Billing.quote_stay(stays, guest)
    raise ArgumentError, result[:status] unless result[:status] == 'ok'
    receipt = 'Fawlty Towers | ' + guest[:name] + ' owes ' + Billing.money(result[:total])
    @receipts << receipt
    receipt
  end

  def save(path)
    File.write(path, JSON.generate(@receipts) + "\n")
    @receipts.length
  end
end
