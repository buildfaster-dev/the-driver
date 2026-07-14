// helper stuff
var fs = require("fs");

module.exports = {
  log: function (msg) {
    console.log("[LOG] " + msg);
    try {
      fs.appendFileSync("log.txt", msg + "\n");
    } catch (e) {}
  },
};
