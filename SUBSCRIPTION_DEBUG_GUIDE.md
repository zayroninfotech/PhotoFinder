# 🔍 Subscription Payment Debug Guide

## If Subscription Renewal is Still Not Working

Follow these steps to diagnose the issue:

---

## **Step 1: Check Browser Console Logs** 🖥️

1. Open browser (Chrome/Firefox)
2. Press **F12** to open Developer Tools
3. Go to **Console** tab
4. Try renewal again
5. Look for messages like:
   ```
   [Payment] Confirming payment: {subscription_id: "...", transaction_id: "...", plan: {...}}
   [Payment] API Response Status: 200
   [Payment] API Response Data: {success: true, message: "...", expires_at: "..."}
   [Payment] ✅ Payment verified successfully
   [Payment] 🔄 Reloading page...
   ```

### **What Each Log Means**:

| Log Message | Means |
|------------|-------|
| `Confirming payment: {...}` | Frontend sending request to API |
| `API Response Status: 200` | ✅ Server responded successfully |
| `API Response Status: 404/500` | ❌ Server error - check Flask logs |
| `API Response Data: {success: true}` | ✅ Payment verified in DB |
| `API Response Data: {success: false}` | ❌ Database update failed |
| `Reloading page...` | ✅ Page will refresh now |

---

## **Step 2: Check Flask Server Logs** 📋

**On your terminal where Flask is running, look for**:

```
[PAYMENT] Verify called: sub_id=sub_xxxxx, txn_id=12345, user_id=user_123
[PAYMENT] Found subscription: True
[PAYMENT] Subscription user_id: user_123, status: pending
[PAYMENT] Verification success: True
[PAYMENT] Updated subscription: {...}
[PAYMENT] User subscription_end: 2026-03-27T00:00:00
```

### **If You See Errors**:

```
[ERROR] subscription_verify: ...
```

This means the API crashed. Check the full error message below it.

---

## **Step 3: Check MongoDB** 📊

**Open MongoDB and run these commands**:

```bash
# Connect to MongoDB
mongosh
use photofinder

# Check if subscription was created
db.subscriptions.find({status: "pending"}).pretty()

# Check if user was updated
db.users.findOne({username: "satya"})

# Should see:
# - subscription_end: 2026-03-27 (future date)
# - is_active: true
```

---

## **Step 4: Common Issues & Fixes**

### **Issue 1: "Subscription not found" Error**

**Cause**: Subscription record wasn't created properly

**Fix**:
1. Check if `/api/subscription/initiate` was called successfully
2. Browser console should show QR code appearing
3. If no QR appears, subscription wasn't created

### **Issue 2: MongoDB Query Fails**

**Cause**: User ID format mismatch

**Current Fix Applied**: Changed from `{"_id": user_id}` to `{"id": user_id}`

**Verify**: Check MongoDB to see if users have an "id" field:
```bash
db.users.findOne({username: "satya"})
# Should show: {_id: ObjectId(...), id: "user_xxxxx", username: "satya", ...}
```

### **Issue 3: Page Reloads but Subscription Still Expired**

**Cause**: Subscription end date wasn't updated

**Check**:
```bash
db.users.findOne({id: "user_xxxxx"})
# Check: subscription_end field should be a FUTURE date
# NOT the past date you set!
```

### **Issue 4: API Returns 500 Error**

**In Flask logs, check the full traceback**:
```
[ERROR] subscription_verify: KeyError: 'some_field'
Traceback (most recent call last):
  ...
```

This tells you exactly which field is missing.

---

## **Testing Step-by-Step** ✅

### **1. Set Expired Subscription**
```bash
db.users.updateOne(
  {username: "satya"},
  {$set: {subscription_end: "2026-03-20"}}  # Yesterday
)
```

### **2. Reload Dashboard**
- Should see warning toast: "Your subscription has expired..."

### **3. Try to Create Event**
- Click "Generate QR Code"
- Should see modal with 3 plans

### **4. Select Plan & Get QR**
- Click ₹1 plan
- QR code should appear
- Open browser console (F12)

### **5. Complete Payment**
- Enter transaction ID: `12345678901234`
- Check checkbox
- Click "Confirm Payment"
- Watch console logs

### **6. Check Logs**
```
[Payment] Confirming payment: {...}
[Payment] API Response Status: 200
[Payment] API Response Data: {success: true, ...}
[Payment] ✅ Payment verified successfully
[Payment] 🔄 Reloading page...
```

### **7. After Reload (2 seconds)**
- Should see "Choose a plan" message (normal subscription modal)
- No more "Subscription Expired" message
- No more warning toast at bottom

### **8. Try to Create Event Again**
- Should work normally! ✅

---

## **If Still Not Working** 🚨

### **Please provide**:
1. Browser console logs (F12 → Console)
2. Flask server logs (last 20 lines)
3. MongoDB query result:
   ```bash
   db.users.findOne({username: "satya"}) # Your username
   db.subscriptions.findOne({status: "pending"}).pretty()
   ```

---

## **Recent Changes Made**

1. ✅ Fixed database query: `{"_id": user_id}` → `{"id": user_id}`
2. ✅ Added console logging to track payment flow
3. ✅ Added Flask logging to debug API
4. ✅ Improved page reload mechanism
5. ✅ Added error handling on reload failure

---

## **Files Modified for Debugging**

1. `admin_dashboard.html` - Added console.log() calls
2. `app.py` - Added print() logging to verify endpoint

---

**Status**: 🔍 Diagnostics in place - run the testing steps above and check the logs!
