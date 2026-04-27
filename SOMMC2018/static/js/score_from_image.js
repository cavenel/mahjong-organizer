function sortWithIndeces(toSort) {
    var len = toSort.length;
    var indices = new Array(len);
    for (var i = 0; i < len; ++i) indices[i] = i;
    indices.sort(function (a, b) { return toSort[a] < toSort[b] ? 1 : toSort[a] > toSort[b] ? -1 : 0; });
    return indices;
}

function get_tp (minipoints) {
    var indeces = sortWithIndeces(minipoints);
    console.log(minipoints, indeces)
    table_points = [4,2,1,0];
    tp = [null,null,null,null];
    table_points_to_give = [];
    players_to_give = [];
    max_mp = 100000000;

    for (var i = 0; i < indeces.length; ++i) {
    if (minipoints[indeces[i]] != max_mp) {
        players_to_give.push([])
        table_points_to_give.push([])
        max_mp = minipoints[indeces[i]];
    }
    players_to_give[players_to_give.length-1].push(indeces[i]);
    table_points_to_give[table_points_to_give.length-1].push(table_points[i]);
    }
    for (var i = 0; i < players_to_give.length; ++i) {
    var sum = table_points_to_give[i].reduce(function(a, b) { return a + b; });
    var avg = sum / table_points_to_give[i].length;
    for (var j = 0; j < players_to_give[i].length; ++j) {
        tp[players_to_give[i][j]] = avg;
    }
    }
    console.log(players_to_give, table_points_to_give, tp);
    return tp;
}

function update_scores () {
    console.log("update scores !");
    total = [0,0,0,0]
    for (i = 0; i < 16; i++) { 
        
        if ($("#pts_" + (i+1)).val() == "0") {
            $("#pts_" + (i+1)).css("background-color","#e6b9b8");
        }
        else {
            $("#pts_" + (i+1)).css("background-color","#d7e4bd");
        }
        if ($("#by_" + (i+1)).val() == "0") {
            $("#by_" + (i+1)).css("background-color","#e6b9b8");
        }
        else {
            $("#by_" + (i+1)).css("background-color","#d7e4bd");
        }
        if ($("#from_" + (i+1)).val() == "0") {
            $("#from_" + (i+1)).css("background-color","#e6b9b8");
        }
        else {
            $("#from_" + (i+1)).css("background-color","#d7e4bd");
        }
        
        pts = parseInt($("#pts_" + (i+1)).val());
        by = parseInt($("#by_" + (i+1)).val());
        from = parseInt($("#from_" + (i+1)).val());
        console.log(pts, by, from);
        if (isNaN(from))
            from = 0;
        if (isNaN(by) || by < 1 || by > 4 || from > 4) {
            for (p = 1; p < 5; p++) {
                $("#pts_" + (i+1) + "_" + p).html("&nbsp;")
                $("#total_" + (i+1) + "_" + p).html(total[p-1])
            } 
            continue;
        }
        if (from == 0 || from == by) {
            for (p = 1; p < 5; p++) {
                if (p == by) {
                    $("#pts_" + (i+1) + "_" + p).html(3*(8+pts))
                    //$("#pts_" + (i+1) + "_" + p).css("background-color","#bdd7e4");
                    //$("#total_" + (i+1) + "_" + p).css("background-color","#bdd7e4");
                }
                else {
                    $("#pts_" + (i+1) + "_" + p).html(-8-pts)
                }
            } 
        }
        else {
            for (p = 1; p < 5; p++) {
                if (p == by) {
                    $("#pts_" + (i+1) + "_" + p).html(3*8+pts);
                    //$("#pts_" + (i+1) + "_" + p).css("background-color","#d7e4bd");
                    //$("#total_" + (i+1) + "_" + p).css("background-color","#d7e4bd");
                }
                else if (p == from) {
                    $("#pts_" + (i+1) + "_" + p).html(-8-pts);
                    //$("#pts_" + (i+1) + "_" + p).css("background-color","#e6b9b8");
                    //$("#total_" + (i+1) + "_" + p).css("background-color","#e6b9b8");
                }
                else {
                    $("#pts_" + (i+1) + "_" + p).html(-8);
                }
            } 
        }

        for (p = 1; p < 5; p++) {
            total[p-1] += parseInt($("#pts_" + (i+1) + "_" + p).html());
            $("#total_" + (i+1) + "_" + p).html(total[p-1]);
        }

    }
    for (p = 1; p < 5; p++) {
        $("#total_" + p).html(total[p-1]);
    } 
    tp = get_tp (total);
    
    for (p = 1; p < 5; p++) {
        $("#tp_" + p).html(tp[p-1]);
    } 
}

document.querySelector('ons-back-button').onClick = function(event) {
    // Reset the whole stack instead of popping 1 page
    appNavigator.popPage();
    window.history.go(-1);
};
$(document).on('change', "input", function(){
    update_scores();
    var input_actual = $(this);
    var found = false;
    var done = false;
    for (i = 0; i < 16; i++) { 
        var hand = i+1;
        console.log((`[data-hand_nb='${hand}']`));
        $("body").find(`[data-hand_nb='${hand}']`).each(function( index ) {
            console.log(input_actual, $(this), found, done);
            if (input_actual.attr("id") == $(this).attr("id")) {
                found = true;
            }
            else if (!done && found && $(this).val() == "0") {
                $(this).focus();
                done = true;
            }
        });
    }
})
$(document).on('focus', "input", function () {
    $(this).select().mouseup(function (e) {
        e.preventDefault();
        $(this).unbind("mouseup");
    });
 });
setTimeout(update_scores, 500);

$(document).on('submit', "#hand_form", function(event){
    event.preventDefault();
    data = $(this).serialize();
    console.log(data);
    $.ajax({
            url:'create_hand_points',
            type:'POST',
            data:$(this).serialize(),
            success:function(result){
                appNavigator.popPage();
                window.history.go(-1);
            }

    });
});