# Basil has put every hotel tariff in one place. It is not a small place.
require_relative 'bookings'

module Billing
  def self.money(pennies)
    format('£%d.%02d', pennies / 100, pennies % 100)
  end

  def self.quote_stay(stays, guest, coupon = '', statement = 'email')
    return { status: 'empty', total: 0 } if stays.empty?
    return { status: 'blocked', total: 0 } if guest[:blocked]
    total = 0
    notes = []
    stays.each do |item|
      if item.key?(:room_type)
        if item.key?(:nightly_rate)
          if item[:nightly_rate] >= 0
            if item.fetch(:room_count, 1) > 0
              rate_pennies = item[:nightly_rate] * item.fetch(:room_count, 1)
              if guest[:tier] == 'gold'
                if item[:wing] == 'east'
                  if item.fetch(:room_count, 1) >= 3
                    rate_pennies = rate_pennies * 80 / 100
                    notes << 'gold east bulk'
                  else
                    rate_pennies = rate_pennies * 90 / 100
                    notes << 'gold east'
                  end
                else
                  rate_pennies = rate_pennies * 95 / 100
                  notes << 'gold general'
                end
              elsif guest[:tier] == 'silver'
                if item.fetch(:room_count, 1) >= 2
                  rate_pennies = rate_pennies * 95 / 100
                  notes << 'silver bulk'
                else
                  notes << 'silver single'
                end
              else
                if item[:off_peak]
                  rate_pennies = rate_pennies * 70 / 100
                  notes << 'off_peak'
                else
                  notes << 'standard'
                end
              end
              total += rate_pennies
            else
              return { status: 'invalid room count', total: 0 }
            end
          else
            return { status: 'invalid room rate', total: 0 }
          end
        else
          return { status: 'missing room rate', total: 0 }
        end
      elsif item.key?(:room_code)
        if item.key?(:rate_pennies)
          if item[:rate_pennies] >= 0
            rate_pennies = item[:rate_pennies] * item.fetch(:room_count, 1)
            if item[:off_peak]
              rate_pennies = rate_pennies * 70 / 100
              notes << 'legacy off_peak'
            end
            total += rate_pennies
          else
            return { status: 'invalid annex rate', total: 0 }
          end
        else
          return { status: 'missing annex rate', total: 0 }
        end
      else
        return { status: 'unknown room', total: 0 }
      end
    end
    unless coupon.empty?
      if coupon == 'SAVE10'
        if total >= 2000
          total -= 200
          notes << 'coupon'
        else
          notes << 'coupon below minimum'
        end
      elsif coupon == 'STAFF'
        if guest[:staff]
          total = total * 85 / 100
        else
          notes << 'staff coupon refused'
        end
      else
        notes << 'unknown coupon'
      end
    end
    if statement == 'post'
      if total < 5000
        if guest[:tier] == 'gold'
          total += 250
        else
          total += 500
        end
      else
        notes << 'free postage'
      end
    elsif statement != 'email'
      return { status: 'unknown statement', total: 0 }
    end
    { status: 'ok', total: total, notes: notes }
  end

  def self.quote_saved_booking(id)
    booking = Bookings.lookup(id)
    quote_stay(booking[:stays], booking[:guest])
  end
end
