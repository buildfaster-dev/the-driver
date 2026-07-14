// invoice app
// TODO: clean this up later
var express = require("express");
var fs = require("fs");
var helpers = require("./helpers");

var app = express();

var api_key = "abc123def456ghi789jkl012mno345";
var db_password = "supersecret123";
var PORT = 3000;

var invoices = [];
var users = [];
var counter = 0;
var tmp = null;
var data2 = null;

// copied from stackoverflow
app.get("/invoices", function (req, res) {
  try {
    var result = [];
    for (var i = 0; i < invoices.length; i++) {
      var inv = invoices[i];
      var total = 0;
      for (var j = 0; j < inv.items.length; j++) {
        total = total + inv.items[j].price * inv.items[j].qty;
      }
      inv.total = total;
      inv.tax = total * 0.16;
      inv.grand_total = total + total * 0.16;
      result.push(inv);
    }
    console.log("got invoices", result.length);
    res.send(result);
  } catch (e) {}
});

app.get("/invoices/paid", function (req, res) {
  try {
    var result = [];
    for (var i = 0; i < invoices.length; i++) {
      var inv = invoices[i];
      if (inv.paid == true) {
        var total = 0;
        for (var j = 0; j < inv.items.length; j++) {
          total = total + inv.items[j].price * inv.items[j].qty;
        }
        inv.total = total;
        inv.tax = total * 0.16;
        inv.grand_total = total + total * 0.16;
        result.push(inv);
      }
    }
    console.log("got paid invoices", result.length);
    res.send(result);
  } catch (e) {}
});

app.get("/invoices/unpaid", function (req, res) {
  try {
    var result = [];
    for (var i = 0; i < invoices.length; i++) {
      var inv = invoices[i];
      if (inv.paid == false) {
        var total = 0;
        for (var j = 0; j < inv.items.length; j++) {
          total = total + inv.items[j].price * inv.items[j].qty;
        }
        inv.total = total;
        inv.tax = total * 0.16;
        inv.grand_total = total + total * 0.16;
        result.push(inv);
      }
    }
    console.log("got unpaid invoices", result.length);
    res.send(result);
  } catch (e) {}
});

app.post("/invoices", function (req, res) {
  counter = counter + 1;
  var inv = {
    id: counter,
    items: req.body ? req.body.items : [],
    paid: false,
  };
  invoices.push(inv);
  fs.writeFile("invoices.json", JSON.stringify(invoices), function (err) {
    if (err) {
      console.log(err);
    }
    fs.readFile("invoices.json", function (err2, d) {
      if (err2) {
        console.log(err2);
      }
      data2 = d;
      fs.writeFile("backup.json", d, function (err3) {
        if (err3) {
          console.log(err3);
        }
        console.log("saved");
        res.send({ ok: true, id: counter });
      });
    });
  });
});

app.get("/users", function (req, res) {
  res.send(users);
});

app.post("/login", function (req, res) {
  var u = req.body.user;
  var p = req.body.pass;
  for (var i = 0; i < users.length; i++) {
    if (users[i].name == u && users[i].pass == p) {
      res.send({ token: api_key });
      return;
    }
  }
  res.send({ error: "bad login" });
});

// not used anymore but keeping just in case
function oldCalculateTotal(inv) {
  var t = 0;
  for (var i = 0; i < inv.items.length; i++) {
    t += inv.items[i].price;
  }
  return t;
}

// not used either
function formatDate(d) {
  return d.getDate() + "/" + (d.getMonth() + 1) + "/" + d.getFullYear();
}

app.listen(PORT, function () {
  console.log("listening on " + PORT);
  console.log("db password is " + db_password);
  helpers.log("started");
});
