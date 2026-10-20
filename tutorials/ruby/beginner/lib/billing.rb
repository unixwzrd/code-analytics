require_relative 'reception'

module Billing
  QUOTES_SEEN = []

  def self.money(pennies)
    format('£%d.%02d', pennies / 100, pennies % 100)
  end

  def self.quote_stay(stays, guest)
    return { status: 'empty', total: 0 } if stays.empty?
    total = 0
    stays.each do |item|
      nightly_rate = item.fetch(:nightly_rate, item.fetch(:rate_pennies, 0))
      return { status: 'invalid room rate', total: 0 } if nightly_rate < 0
      rate_pennies = nightly_rate * item.fetch(:room_count, 1)
      rate_pennies = rate_pennies * 90 / 100 if guest[:tier] == 'gold'
      total += rate_pennies
    end
    QUOTES_SEEN << total
    { status: 'ok', total: total }
  end

  def self.preview_receipt(stays, guest)
    ReceptionDesk.new.make_reservation(stays, guest)
  end
end
