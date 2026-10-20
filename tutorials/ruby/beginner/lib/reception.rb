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

  def make_reservation(stays, guest, receipt_path = nil, channel = 'desk', breakfast = false, quiet = false, porter = false)
    # Basil regards argument lists as an opportunity to avoid more methods.
    raise ArgumentError, 'missing guest name' unless guest.key?(:name)
    raise ArgumentError, 'invalid guest name' unless guest[:name].is_a?(String)
    raise ArgumentError, 'blank guest name' if guest[:name].strip.empty?
    raise ArgumentError, 'empty reservation' if stays.empty?
    count = 0
    instructions = []
    stays.each do |stay|
      if stay.key?(:nightly_rate)
        raise ArgumentError, 'negative nightly rate' if stay[:nightly_rate] < 0
        raise ArgumentError, 'invalid room count' if stay.fetch(:room_count, 1) < 1
        count += stay.fetch(:room_count, 1)
      elsif stay.key?(:rate_pennies)
        raise ArgumentError, 'negative annex rate' if stay[:rate_pennies] < 0
        count += stay.fetch(:room_count, 1)
      else
        raise ArgumentError, 'missing room rate'
      end
    end
    if channel == 'desk'
      if quiet
        instructions << 'Polly: use the garden corridor'
      else
        instructions << 'Basil: the desk is already quiet'
      end
    elsif channel == 'telephone'
      instructions << 'Manuel: telephone reservation copied once'
    else
      raise ArgumentError, 'unknown reservation channel'
    end
    if breakfast
      if count > 1
        instructions << 'Polly: two breakfast places, one argument'
      else
        instructions << 'Polly: one breakfast place'
      end
    end
    instructions << 'Manuel: luggage noted; no extra charge' if porter
    result = Billing.quote_stay(stays, guest)
    raise ArgumentError, result[:status] unless result[:status] == 'ok'
    receipt = 'Fawlty Towers | ' + guest[:name] + ' owes ' + Billing.money(result[:total])
    @receipts << receipt
    @last_instructions = instructions
    save(receipt_path) unless receipt_path.nil?
    receipt
  end

  def save(path)
    File.write(path, JSON.generate(@receipts) + "\n")
    @receipts.length
  end
end
