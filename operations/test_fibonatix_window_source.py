import csv,io,unittest,copy
from operations.fibonatix_window_source import parse,timezone_offset,STATUS

def csv_bytes(rows):
 s=io.StringIO();w=csv.DictWriter(s,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows);return s.getvalue().encode()
def fixture():
 rows=[{'TRX ID':'TEST00'+str(i),'Brand':'TheDrops','Currency':'EUR',STATUS:'Approved','Status Code':'20000','Type':'SL','Display Time':'2026-10-05 20:00:00','Brand TRX ID':str(100+i),'Order Description':'Order #'+str(100+i),'Amount':'10.00','MDR Fee':'','TRX Fee':''} for i in range(3)]
 orders=[{'order_id':100+i,'order_number':str(200+i),'payment_method':'wc_fibonatix','currency':'EUR','status':'completed'} for i in range(3)]
 return rows,orders
class Source(unittest.TestCase):
 def test_own_order_number_not_internal_id(self):
  r,o=fixture();a,s=parse(csv_bytes(r),o,offset_hours=2,allowed_dates={'2026-10-05'})
  self.assertEqual(a[0]['ref'],'TD200');self.assertEqual(s['total'],'30.00');self.assertFalse(a[0]['fees_known'])
 def test_cutoff_conversion(self):
  r,o=fixture();r[0]['Display Time']='2026-10-05 23:00:00'
  a,s=parse(csv_bytes(r),o,offset_hours=2,allowed_dates={'2026-10-05'})
  self.assertEqual(len(a),2);self.assertEqual(s['outside_ids'],['TEST000'])
 def test_declined_not_booked(self):
  r,o=fixture();r[0][STATUS]='Declined';a,s=parse(csv_bytes(r),o,offset_hours=0,allowed_dates={'2026-10-05'});self.assertEqual(len(a),2)
 def test_bad_identity_or_amount_blocks(self):
  for key,value in [('Brand','Other'),('Currency','USD'),('Type','RF'),('Amount','NaN'),('Order Description','Order #999'),('Brand TRX ID','999')]:
   r,o=fixture();r[0][key]=value
   with self.assertRaises(ValueError):parse(csv_bytes(r),o,offset_hours=0,allowed_dates={'2026-10-05'})
 def test_duplicate_ids_and_orders_block(self):
  r,o=fixture();r.append(copy.deepcopy(r[0]))
  with self.assertRaises(ValueError):parse(csv_bytes(r),o,offset_hours=0,allowed_dates={'2026-10-05'})
  r,o=fixture();o[1]['order_number']=o[0]['order_number']
  with self.assertRaises(ValueError):parse(csv_bytes(r),o,offset_hours=0,allowed_dates={'2026-10-05'})
 def test_timezone_requires_actual_correlated_psp_ids(self):
  r,_=fixture();raw=csv_bytes(r);e=[(x['TRX ID'],'2026-10-05T22:00:00') for x in r]
  self.assertEqual(timezone_offset(raw,e),2)
  with self.assertRaises(ValueError):timezone_offset(raw,e[:2])
  e[-1]=(e[-1][0],'2026-10-05T21:00:00')
  with self.assertRaises(ValueError):timezone_offset(raw,e)
if __name__=='__main__':unittest.main()
